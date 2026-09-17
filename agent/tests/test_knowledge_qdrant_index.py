import unittest
from types import SimpleNamespace

from app.knowledge import (
    KNOWLEDGE_PAYLOAD_INDEXES,
    KnowledgeChunk,
    ensure_knowledge_collection,
    knowledge_chunk_payload,
    knowledge_chunk_to_point,
    knowledge_point_id,
)
from app.rag import EMBEDDING_DIMENSION
from app.settings import settings


class FakeQdrantClient:
    def __init__(self, *, exists=False, payload_schema=None):
        self.exists = exists
        self.payload_schema = payload_schema or {}
        self.created_collections = []
        self.created_indexes = []

    def collection_exists(self, collection_name):
        return self.exists

    def create_collection(self, **kwargs):
        self.created_collections.append(kwargs)
        self.exists = True

    def get_collection(self, collection_name):
        return SimpleNamespace(payload_schema=self.payload_schema)

    def create_payload_index(self, **kwargs):
        self.created_indexes.append(kwargs)


def sample_chunk():
    return KnowledgeChunk(
        chunkId="review:review-005",
        sourceType="review",
        sourceId="review-005",
        content="店里有插座，下午适合办公。",
        metadata={"shopId": 3, "typeId": 2},
        tags=["office"],
    )


class KnowledgeQdrantIndexTests(unittest.TestCase):
    def test_point_id_is_stable_and_changes_with_chunk_identity(self):
        first = knowledge_point_id("review:review-005")

        self.assertEqual(first, knowledge_point_id("review:review-005"))
        self.assertNotEqual(first, knowledge_point_id("review:review-006"))

    def test_payload_uses_aliases_and_preserves_nested_metadata(self):
        payload = knowledge_chunk_payload(sample_chunk())

        self.assertEqual(payload["chunkId"], "review:review-005")
        self.assertEqual(payload["sourceType"], "review")
        self.assertEqual(payload["metadata"]["shopId"], 3)
        self.assertEqual(len(payload["contentHash"]), 64)
        self.assertNotIn("chunk_id", payload)

    def test_point_contains_stable_id_vector_and_payload(self):
        chunk = sample_chunk()
        point = knowledge_chunk_to_point(
            chunk,
            [0.0] * EMBEDDING_DIMENSION,
        )

        self.assertEqual(point.id, knowledge_point_id(chunk.chunk_id))
        self.assertEqual(len(point.vector), EMBEDDING_DIMENSION)
        self.assertEqual(point.payload["chunkId"], chunk.chunk_id)

    def test_point_rejects_wrong_dimension_and_non_finite_values(self):
        with self.assertRaisesRegex(ValueError, "dimensions"):
            knowledge_chunk_to_point(sample_chunk(), [0.0])
        with self.assertRaisesRegex(ValueError, "finite"):
            knowledge_chunk_to_point(
                sample_chunk(),
                [float("nan"), *([0.0] * (EMBEDDING_DIMENSION - 1))],
            )

    def test_ensure_collection_creates_collection_and_missing_indexes(self):
        client = FakeQdrantClient(
            payload_schema={"sourceType": object()},
        )

        ensure_knowledge_collection(client)

        self.assertEqual(len(client.created_collections), 1)
        collection = client.created_collections[0]
        self.assertEqual(
            collection["collection_name"],
            settings.knowledge_collection_name,
        )
        self.assertEqual(collection["vectors_config"].size, EMBEDDING_DIMENSION)
        created_fields = {
            item["field_name"] for item in client.created_indexes
        }
        self.assertEqual(
            created_fields,
            set(KNOWLEDGE_PAYLOAD_INDEXES) - {"sourceType"},
        )


if __name__ == "__main__":
    unittest.main()
