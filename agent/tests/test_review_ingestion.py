import unittest
from unittest.mock import Mock, patch

from app.rag import ingest_reviews, load_reviews, search_reviews


class FakeVector:
    def __init__(self, values):
        self.values = values

    def tolist(self):
        return self.values


class ReviewIngestionTests(unittest.TestCase):
    @patch("app.rag.httpx.get")
    def test_load_reviews_reads_and_normalizes_backend_response(self, get_mock):
        response = Mock()
        response.json.return_value = {
            "success": True,
            "data": [
                {
                    "reviewId": "review-005",
                    "shopId": 3,
                    "shopName": "清晨手冲咖啡",
                    "content": "适合带电脑办公。",
                    "source": "seed",
                    "language": "zh",
                    "contentZh": None,
                    "translationStatus": "not_required",
                    "tags": ["办公", "插座"],
                }
            ],
        }
        get_mock.return_value = response

        reviews = load_reviews()

        self.assertEqual(
            reviews,
            [
                {
                    "id": "review-005",
                    "reviewId": "review-005",
                    "shopId": 3,
                    "shopName": "清晨手冲咖啡",
                    "text": "适合带电脑办公。",
                    "embeddingText": "适合带电脑办公。",
                    "originalText": "适合带电脑办公。",
                    "contentZh": None,
                    "source": "seed",
                    "language": "zh",
                    "sourceLanguage": "zh",
                    "translationStatus": "not_required",
                    "tags": ["办公", "插座"],
                }
            ],
        )
        get_mock.assert_called_once()
        self.assertTrue(get_mock.call_args.args[0].endswith("/api/reviews"))
        response.raise_for_status.assert_called_once_with()

    @patch("app.rag.get_qdrant_client")
    @patch("app.rag.get_embedding_model")
    @patch("app.rag.load_reviews")
    def test_ingest_reviews_rebuilds_collection_from_backend_reviews(
        self,
        load_mock,
        model_mock,
        client_mock,
    ):
        load_mock.return_value = [
            {
                "id": "review-005",
                "shopId": 3,
                "shopName": "清晨手冲咖啡",
                "text": "适合带电脑办公。",
                "tags": ["办公", "插座"],
            }
        ]
        model_mock.return_value.embed.return_value = iter([FakeVector([0.1, 0.2])])
        client_mock.return_value.collection_exists.side_effect = [True, False]

        count = ingest_reviews()

        self.assertEqual(count, 1)
        client_mock.return_value.delete_collection.assert_called_once()
        client_mock.return_value.create_collection.assert_called_once()
        point = client_mock.return_value.upsert.call_args.kwargs["points"][0]
        self.assertEqual(point.payload["reviewId"], "review-005")
        self.assertEqual(point.payload["shopName"], "清晨手冲咖啡")
        self.assertEqual(point.payload["originalText"], "适合带电脑办公。")
        self.assertEqual(point.payload["source"], "seed")

    @patch("app.rag.httpx.get")
    def test_load_reviews_marks_translated_text_as_zh_and_preserves_source_language(
        self,
        get_mock,
    ):
        response = Mock()
        response.json.return_value = {
            "success": True,
            "data": [
                {
                    "reviewId": "yelp-001",
                    "shopId": 7,
                    "shopName": "Example Shop",
                    "content": "A quiet place.",
                    "contentZh": "一个安静的地方。",
                    "source": "yelp",
                    "language": "en",
                    "translationStatus": "translated",
                    "tags": [],
                }
            ],
        }
        get_mock.return_value = response

        review = load_reviews()[0]

        self.assertEqual(review["text"], "一个安静的地方。")
        self.assertEqual(review["language"], "zh")
        self.assertEqual(review["sourceLanguage"], "en")

    @patch("app.rag.get_qdrant_client")
    @patch("app.rag.get_embedding_model")
    @patch("app.rag.load_reviews", return_value=[])
    def test_ingest_reviews_can_rebuild_an_empty_collection(
        self,
        _load_mock,
        model_mock,
        client_mock,
    ):
        client_mock.return_value.collection_exists.side_effect = [True, False]

        count = ingest_reviews()

        self.assertEqual(count, 0)
        model_mock.assert_not_called()
        client_mock.return_value.delete_collection.assert_called_once()
        client_mock.return_value.create_collection.assert_called_once()
        client_mock.return_value.upsert.assert_not_called()

    @patch("app.rag.INGEST_BATCH_SIZE", 2)
    @patch("app.rag.get_qdrant_client")
    @patch("app.rag.get_embedding_model")
    @patch("app.rag.load_reviews")
    def test_ingest_reviews_upserts_points_in_batches(
        self,
        load_mock,
        model_mock,
        client_mock,
    ):
        load_mock.return_value = [
            {
                "id": f"review-batch-{index}",
                "shopId": 3,
                "shopName": "清晨手冲咖啡",
                "text": f"第 {index} 条评论",
                "tags": ["测试"],
            }
            for index in range(3)
        ]
        model_mock.return_value.embed.return_value = iter(
            [
                FakeVector([0.1, 0.2]),
                FakeVector([0.3, 0.4]),
                FakeVector([0.5, 0.6]),
            ]
        )
        client_mock.return_value.collection_exists.side_effect = [True, False]

        count = ingest_reviews()

        self.assertEqual(count, 3)
        self.assertEqual(client_mock.return_value.upsert.call_count, 2)
        first_batch = client_mock.return_value.upsert.call_args_list[0].kwargs["points"]
        second_batch = client_mock.return_value.upsert.call_args_list[1].kwargs["points"]
        self.assertEqual(len(first_batch), 2)
        self.assertEqual(len(second_batch), 1)

    @patch("app.rag.get_qdrant_client")
    @patch("app.rag.get_embedding_model")
    def test_search_reviews_can_filter_by_source(self, model_mock, client_mock):
        model_mock.return_value.embed.return_value = iter([FakeVector([0.1, 0.2])])
        client_mock.return_value.query_points.return_value.points = []

        search_reviews("咖啡店适合办公吗？", limit=3, source="yelp")

        query_filter = client_mock.return_value.query_points.call_args.kwargs["query_filter"]
        self.assertEqual(query_filter.must[0].key, "source")
        self.assertEqual(query_filter.must[0].match.value, "yelp")

    @patch("app.rag.get_qdrant_client")
    @patch("app.rag.get_embedding_model")
    def test_search_reviews_combines_source_and_shop_filters(self, model_mock, client_mock):
        model_mock.return_value.embed.return_value = iter([FakeVector([0.1, 0.2])])
        client_mock.return_value.query_points.return_value.points = []

        search_reviews(
            "Red Hook 适合办公吗？",
            limit=3,
            source="yelp",
            shop_id=100011,
        )

        query_filter = client_mock.return_value.query_points.call_args.kwargs["query_filter"]
        conditions = {
            condition.key: condition.match.value
            for condition in query_filter.must
        }
        self.assertEqual(
            conditions,
            {"source": "yelp", "shopId": 100011},
        )

    def test_search_reviews_rejects_non_positive_shop_id(self):
        with self.assertRaisesRegex(ValueError, "shop_id must be positive"):
            search_reviews("query", shop_id=0)

    @patch("app.rag.get_qdrant_client")
    @patch("app.rag.get_embedding_model")
    def test_search_reviews_filters_by_candidate_shop_ids(
        self,
        model_mock,
        client_mock,
    ):
        model_mock.return_value.embed.return_value = iter([FakeVector([0.1, 0.2])])
        client_mock.return_value.query_points.return_value.points = []

        search_reviews(
            "哪家店更安静？",
            limit=3,
            source="yelp",
            shop_ids=[7, 9, 7],
        )

        query_filter = client_mock.return_value.query_points.call_args.kwargs["query_filter"]
        conditions = {condition.key: condition for condition in query_filter.must}
        self.assertEqual(conditions["source"].match.value, "yelp")
        self.assertEqual(conditions["shopId"].match.any, [7, 9])

    @patch("app.rag.get_qdrant_client")
    @patch("app.rag.get_embedding_model")
    def test_search_reviews_empty_candidate_set_returns_no_results(
        self,
        model_mock,
        client_mock,
    ):
        self.assertEqual(search_reviews("query", shop_ids=[]), [])
        model_mock.assert_not_called()
        client_mock.assert_not_called()

    def test_search_reviews_rejects_single_and_multiple_shop_filters(self):
        with self.assertRaisesRegex(
            ValueError,
            "shop_id and shop_ids cannot be used together",
        ):
            search_reviews("query", shop_id=7, shop_ids=[7, 9])

    def test_search_reviews_rejects_non_positive_candidate_shop_id(self):
        with self.assertRaisesRegex(
            ValueError,
            "shop_ids must contain only positive integers",
        ):
            search_reviews("query", shop_ids=[7, 0])


if __name__ == "__main__":
    unittest.main()
