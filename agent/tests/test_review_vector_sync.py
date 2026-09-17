import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.rag import delete_review_vector, review_point_id, upsert_review_vector


class FakeVector:
    def tolist(self):
        return [0.1, 0.2, 0.3]


class ReviewVectorFunctionsTests(unittest.TestCase):
    def test_review_point_id_preserves_seed_ids_and_stabilizes_business_ids(self):
        self.assertEqual(review_point_id("review-005"), 5)
        generated_id = review_point_id("review-business-123")

        self.assertEqual(generated_id, review_point_id("review-business-123"))
        uuid.UUID(generated_id)

    @patch("app.rag.get_qdrant_client")
    @patch("app.rag.get_embedding_model")
    def test_upsert_review_vector_writes_vector_and_payload(self, model_mock, client_mock):
        model_mock.return_value.embed.return_value = iter([FakeVector()])
        client_mock.return_value.collection_exists.return_value = True

        upsert_review_vector(
            {
                "reviewId": "review-business-123",
                "shopId": 3,
                "shopName": "清晨手冲咖啡",
                "content": "新增的办公体验评论",
                "tags": ["办公", "插座"],
            }
        )

        point = client_mock.return_value.upsert.call_args.kwargs["points"][0]
        self.assertEqual(point.id, review_point_id("review-business-123"))
        self.assertEqual(point.payload["text"], "新增的办公体验评论")
        self.assertEqual(point.payload["shopName"], "清晨手冲咖啡")

    @patch("app.rag.get_qdrant_client")
    def test_delete_review_vector_uses_same_point_id(self, client_mock):
        client_mock.return_value.collection_exists.return_value = True

        delete_review_vector("review-business-123")

        selector = client_mock.return_value.delete.call_args.kwargs["points_selector"]
        self.assertEqual(selector.points, [review_point_id("review-business-123")])


class ReviewVectorEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("app.main.upsert_review_search_indexes")
    def test_upsert_endpoint_returns_sync_result(self, upsert_mock):
        response = self.client.put(
            "/internal/review-vectors/review-business-123",
            json={
                "shopId": 3,
                "shopName": "清晨手冲咖啡",
                "content": "新增的办公体验评论",
                "tags": ["办公", "插座"],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "reviewId": "review-business-123",
                "operation": "upsert",
                "synced": True,
            },
        )
        upsert_mock.assert_called_once()

    @patch("app.main.delete_review_search_indexes")
    def test_delete_endpoint_returns_sync_result(self, delete_mock):
        response = self.client.delete(
            "/internal/review-vectors/review-business-123"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["operation"], "delete")
        self.assertTrue(response.json()["synced"])
        delete_mock.assert_called_once_with("review-business-123")

    @patch(
        "app.main.upsert_review_search_indexes",
        side_effect=ConnectionError("secret-qdrant"),
    )
    def test_upsert_endpoint_returns_safe_503(self, _upsert_mock):
        response = self.client.put(
            "/internal/review-vectors/review-business-123",
            json={
                "shopId": 3,
                "shopName": "清晨手冲咖啡",
                "content": "新增的办公体验评论",
                "tags": [],
            },
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "评论向量同步失败")
        self.assertNotIn("secret", response.text)


if __name__ == "__main__":
    unittest.main()
