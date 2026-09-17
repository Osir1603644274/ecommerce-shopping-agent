import unittest
from types import SimpleNamespace

from app.knowledge import (
    MERCHANT_DOC_SOURCE_NAME,
    REVIEW_SOURCE_NAME,
    search_knowledge_index,
)
from app.settings import settings


def payload(chunk_id, source_type, source_id, shop_id):
    return {
        "chunkId": chunk_id,
        "sourceType": source_type,
        "sourceId": source_id,
        "content": "evidence",
        "metadata": {"shopId": shop_id},
        "visibility": "public",
    }


class FakeClient:
    def __init__(self):
        self.calls = []

    def query_points(self, **kwargs):
        self.calls.append(kwargs)
        source_type = kwargs["query_filter"].must[1].match.value
        point = SimpleNamespace(
            payload=payload(
                f"{source_type}:target",
                source_type,
                "target",
                7,
            ),
            score=0.82,
        )
        return SimpleNamespace(points=[point])


class KnowledgeUnifiedSearchTests(unittest.TestCase):
    def test_searches_each_source_with_one_shared_query_embedding(self):
        client = FakeClient()
        embed_calls = []

        def embedder(texts):
            embed_calls.append(list(texts))
            return [[0.1, 0.2]]

        result = search_knowledge_index(
            "quiet place",
            sources=[MERCHANT_DOC_SOURCE_NAME, REVIEW_SOURCE_NAME],
            limit=3,
            client=client,
            embedder=embedder,
        )

        self.assertEqual(embed_calls, [["quiet place"]])
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(
            all(
                call["collection_name"] == settings.knowledge_collection_name
                for call in client.calls
            )
        )
        self.assertEqual(len(result.chunks), 2)
        self.assertEqual(len(result.citations), 2)
        self.assertEqual(result.trace.returned_count, 2)

    def test_shop_ids_are_deduplicated_and_added_to_qdrant_filter(self):
        client = FakeClient()

        result = search_knowledge_index(
            "quiet",
            sources=[REVIEW_SOURCE_NAME],
            shop_ids=[7, 7, 9],
            client=client,
            embedder=lambda _texts: [[0.1]],
        )

        match_any = client.calls[0]["query_filter"].must[2].match.any
        self.assertEqual(match_any, [7, 9])
        self.assertEqual(result.trace.filters["shopIds"], [7, 9])

    def test_empty_shop_candidates_do_not_fall_back_to_global_search(self):
        client = FakeClient()
        embed_called = False

        def embedder(_texts):
            nonlocal embed_called
            embed_called = True
            return [[0.1]]

        result = search_knowledge_index(
            "quiet",
            sources=[REVIEW_SOURCE_NAME],
            shop_ids=[],
            client=client,
            embedder=embedder,
        )

        self.assertFalse(embed_called)
        self.assertEqual(client.calls, [])
        self.assertEqual(result.chunks, [])
        self.assertEqual(result.trace.filters["shopIds"], [])

    def test_excludes_synthetic_review_sources_from_qdrant(self):
        client = FakeClient()

        result = search_knowledge_index(
            "quiet",
            sources=[REVIEW_SOURCE_NAME],
            excluded_review_sources=("seed",),
            client=client,
            embedder=lambda _texts: [[0.1]],
        )

        excluded = client.calls[0]["query_filter"].must_not[0]
        self.assertEqual(excluded.key, "metadata.source")
        self.assertEqual(excluded.match.any, ["seed"])
        self.assertEqual(result.trace.filters["excludedReviewSources"], ["seed"])

    def test_rejects_invalid_inputs(self):
        client = FakeClient()
        embedder = lambda _texts: [[0.1]]
        with self.assertRaisesRegex(ValueError, "blank"):
            search_knowledge_index(" ", client=client, embedder=embedder)
        with self.assertRaisesRegex(ValueError, "positive"):
            search_knowledge_index("q", limit=0, client=client, embedder=embedder)
        with self.assertRaisesRegex(ValueError, "positive integers"):
            search_knowledge_index(
                "q",
                shop_ids=[0],
                client=client,
                embedder=embedder,
            )


if __name__ == "__main__":
    unittest.main()
