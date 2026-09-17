import unittest
from unittest.mock import Mock, patch

from app.knowledge.qdrant_index import knowledge_point_id
from app.knowledge.review_sync import (
    delete_review_knowledge_chunk,
    upsert_review_knowledge_chunk,
)
from app.review_index_events import apply_review_index_event
from app.rag_bm25 import (
    clear_review_bm25_indexes,
    search_all_reviews_bm25,
    search_reviews_bm25,
    upsert_review_bm25,
)
from app.review_index_sync import (
    delete_review_search_indexes,
    upsert_review_search_indexes,
)


REVIEW = {
    "reviewId": "review-business-123",
    "shopId": 3,
    "shopName": "清晨手冲咖啡",
    "content": "安静办公，有插座",
    "source": "user",
    "language": "zh",
    "tags": ["办公"],
}


class ReviewKnowledgeSyncTests(unittest.TestCase):
    @patch("app.knowledge.review_sync.ensure_knowledge_collection")
    @patch("app.knowledge.review_sync.get_qdrant_client")
    def test_upsert_writes_review_chunk_to_unified_collection(
        self,
        client_mock,
        ensure_mock,
    ):
        upsert_review_knowledge_chunk(REVIEW, vector=[0.1] * 512)

        ensure_mock.assert_called_once_with(client_mock.return_value)
        call = client_mock.return_value.upsert.call_args.kwargs
        self.assertEqual(call["collection_name"], "knowledge_chunks")
        point = call["points"][0]
        self.assertEqual(
            point.id,
            knowledge_point_id("review:review-business-123"),
        )
        self.assertEqual(point.payload["metadata"]["shopId"], 3)
        self.assertEqual(point.payload["content"], "安静办公，有插座")

    @patch("app.knowledge.review_sync.ensure_knowledge_collection")
    @patch("app.knowledge.review_sync.get_qdrant_client")
    def test_delete_uses_stable_unified_point_id(self, client_mock, _ensure_mock):
        delete_review_knowledge_chunk("review-business-123")

        selector = client_mock.return_value.delete.call_args.kwargs[
            "points_selector"
        ]
        self.assertEqual(
            selector.points,
            [knowledge_point_id("review:review-business-123")],
        )


class ReviewSearchIndexSyncTests(unittest.TestCase):
    @patch("app.review_index_sync.publish_review_index_event")
    @patch("app.review_index_sync.upsert_review_knowledge_chunk")
    @patch("app.review_index_sync.upsert_review_vector")
    @patch("app.review_index_sync.get_embedding_model")
    def test_upsert_reuses_one_embedding_and_publishes_after_both_writes(
        self,
        model_mock,
        legacy_mock,
        unified_mock,
        publish_mock,
    ):
        vector = Mock()
        vector.tolist.return_value = [0.1] * 512
        model_mock.return_value.embed.return_value = iter([vector])

        upsert_review_search_indexes(REVIEW)

        legacy_mock.assert_called_once_with(REVIEW, vector=[0.1] * 512)
        unified_mock.assert_called_once_with(REVIEW, vector=[0.1] * 512)
        publish_mock.assert_called_once_with(
            {"operation": "upsert", "review": REVIEW}
        )

    @patch("app.review_index_sync.publish_review_index_event")
    @patch("app.review_index_sync.delete_review_knowledge_chunk")
    @patch("app.review_index_sync.delete_review_vector")
    def test_delete_removes_both_indexes_before_publish(
        self,
        legacy_mock,
        unified_mock,
        publish_mock,
    ):
        delete_review_search_indexes("review-business-123")

        legacy_mock.assert_called_once_with("review-business-123")
        unified_mock.assert_called_once_with("review-business-123")
        publish_mock.assert_called_once_with(
            {"operation": "delete", "reviewId": "review-business-123"}
        )

    @patch("app.review_index_sync.publish_review_index_event")
    @patch(
        "app.review_index_sync.upsert_review_knowledge_chunk",
        side_effect=ConnectionError("unified unavailable"),
    )
    @patch("app.review_index_sync.upsert_review_vector")
    @patch("app.review_index_sync.get_embedding_model")
    def test_does_not_publish_partial_upsert(
        self,
        model_mock,
        _legacy_mock,
        _unified_mock,
        publish_mock,
    ):
        vector = Mock()
        vector.tolist.return_value = [0.1] * 512
        model_mock.return_value.embed.return_value = iter([vector])

        with self.assertRaisesRegex(ConnectionError, "unified unavailable"):
            upsert_review_search_indexes(REVIEW)

        publish_mock.assert_not_called()


class ReviewIndexEventTests(unittest.TestCase):
    @patch("app.review_index_events._publisher")
    def test_publishes_versioned_json_event(self, publisher_mock):
        from app.review_index_events import publish_review_index_event

        publish_review_index_event(
            {"operation": "delete", "reviewId": "review-business-123"}
        )

        channel, payload = publisher_mock.return_value.publish.call_args.args
        self.assertEqual(channel, "agent:review-index-events:v1")
        self.assertIn('"version": 1', payload)
        self.assertIn('"reviewId": "review-business-123"', payload)

    @patch("app.review_index_events.upsert_review_bm25")
    def test_applies_upsert_event(self, upsert_mock):
        apply_review_index_event(
            {"version": 1, "operation": "upsert", "review": REVIEW}
        )
        upsert_mock.assert_called_once_with(REVIEW)

    @patch("app.review_index_events.delete_review_bm25")
    def test_applies_delete_event(self, delete_mock):
        apply_review_index_event(
            {
                "version": 1,
                "operation": "delete",
                "reviewId": "review-business-123",
            }
        )
        delete_mock.assert_called_once_with("review-business-123")

    def test_rejects_unknown_event_version(self):
        with self.assertRaisesRegex(ValueError, "unsupported.*version"):
            apply_review_index_event({"version": 2, "operation": "delete"})


class ReviewBm25FreshnessTests(unittest.TestCase):
    @patch("app.rag_bm25.load_reviews", return_value=[])
    def test_production_bm25_excludes_seed_but_offline_index_keeps_it(
        self,
        _load_mock,
    ):
        clear_review_bm25_indexes()
        try:
            upsert_review_bm25({**REVIEW, "source": "seed"})
            production_results = search_reviews_bm25(REVIEW["content"][-2:], 3)
            offline_results = search_all_reviews_bm25(REVIEW["content"][-2:], 3)

            self.assertEqual(production_results, [])
            self.assertEqual(
                [item["reviewId"] for item in offline_results],
                ["review-business-123"],
            )
        finally:
            clear_review_bm25_indexes()

    @patch("app.rag_bm25.load_reviews", return_value=[])
    def test_user_review_content_field_is_searchable_after_upsert(
        self,
        _load_mock,
    ):
        clear_review_bm25_indexes()
        try:
            upsert_review_bm25(REVIEW)

            results = search_reviews_bm25(
                "插座",
                3,
                shop_ids=[3],
            )

            self.assertEqual(
                [item["reviewId"] for item in results],
                ["review-business-123"],
            )
        finally:
            clear_review_bm25_indexes()


if __name__ == "__main__":
    unittest.main()
