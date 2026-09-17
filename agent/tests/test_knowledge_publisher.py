import unittest
from types import SimpleNamespace

from app.knowledge import (
    KnowledgeChunk,
    rebuild_knowledge_index,
    validate_unique_chunk_ids,
)
from app.rag import EMBEDDING_DIMENSION
from app.settings import settings


class FakeQdrantClient:
    def __init__(self, *, exists=True):
        self.exists = exists
        self.deleted = []
        self.created = []
        self.indexes = []
        self.upserts = []

    def collection_exists(self, collection_name):
        return self.exists

    def delete_collection(self, **kwargs):
        self.deleted.append(kwargs)
        self.exists = False

    def create_collection(self, **kwargs):
        self.created.append(kwargs)
        self.exists = True

    def get_collection(self, collection_name):
        return SimpleNamespace(payload_schema={})

    def create_payload_index(self, **kwargs):
        self.indexes.append(kwargs)

    def upsert(self, **kwargs):
        self.upserts.append(kwargs)


def chunk(chunk_id, source_type="review"):
    return KnowledgeChunk(
        chunkId=chunk_id,
        sourceType=source_type,
        sourceId=chunk_id,
        content=f"content for {chunk_id}",
    )


class KnowledgePublisherTests(unittest.TestCase):
    def test_duplicate_ids_fail_before_rebuilding_collection(self):
        client = FakeQdrantClient()

        with self.assertRaisesRegex(ValueError, "duplicate knowledge chunk IDs"):
            rebuild_knowledge_index(
                [chunk("same"), chunk("same")],
                client=client,
                embedder=lambda texts: [],
            )

        self.assertEqual(client.deleted, [])

    def test_validate_unique_chunk_ids_accepts_distinct_sources(self):
        validate_unique_chunk_ids(
            [chunk("review:1"), chunk("policy:1", "policy_doc")]
        )

    def test_rebuild_batches_points_and_reports_source_counts(self):
        client = FakeQdrantClient()
        chunks = [
            chunk("review:1"),
            chunk("review:2"),
            chunk("policy:1", "policy_doc"),
        ]

        summary = rebuild_knowledge_index(
            chunks,
            client=client,
            embedder=lambda texts: [
                [0.0] * EMBEDDING_DIMENSION for _ in texts
            ],
            batch_size=2,
        )

        self.assertEqual(len(client.deleted), 1)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(len(client.upserts), 2)
        self.assertEqual(
            sum(len(call["points"]) for call in client.upserts),
            3,
        )
        self.assertEqual(
            client.upserts[0]["collection_name"],
            settings.knowledge_collection_name,
        )
        self.assertEqual(summary["indexedCount"], 3)
        self.assertEqual(
            summary["sourceCounts"],
            {"policy_doc": 1, "review": 2},
        )

    def test_rebuild_rejects_embedder_count_mismatch(self):
        with self.assertRaisesRegex(ValueError, "different number of vectors"):
            rebuild_knowledge_index(
                [chunk("review:1")],
                client=FakeQdrantClient(),
                embedder=lambda texts: [],
            )

    def test_rebuild_rejects_invalid_batch_size(self):
        with self.assertRaisesRegex(ValueError, "batch_size must be positive"):
            rebuild_knowledge_index([], batch_size=0)


if __name__ == "__main__":
    unittest.main()
