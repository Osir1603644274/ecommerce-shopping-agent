import hashlib
import unittest

from pydantic import ValidationError

from app.knowledge import (
    Citation,
    KnowledgeChunk,
    RetrievalStep,
    RetrievalTrace,
    SearchKnowledgeResult,
)


class KnowledgeModelTests(unittest.TestCase):
    def test_knowledge_chunk_accepts_camel_case_input_and_serializes_aliases(self):
        chunk = KnowledgeChunk(
            chunkId="review:review-005",
            sourceType="review",
            sourceId="review-005",
            title="清晨手冲咖啡评论",
            content="店里有靠窗的单人位和插座，下午写作业或办公很舒服。",
            metadata={"shopId": 3, "typeId": 2},
            ownerUserId=None,
            tags=["office", "quiet"],
        )

        self.assertEqual(chunk.chunk_id, "review:review-005")
        self.assertEqual(chunk.source_type, "review")
        self.assertEqual(chunk.visibility, "public")
        self.assertEqual(chunk.metadata["shopId"], 3)
        self.assertEqual(chunk.chunk_index, 1)
        self.assertEqual(chunk.source_version, "1")
        self.assertEqual(
            chunk.content_hash,
            hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
        )

        serialized = chunk.model_dump(by_alias=True)
        self.assertIn("chunkId", serialized)
        self.assertIn("sourceType", serialized)
        self.assertIn("sourceId", serialized)
        self.assertIn("ownerUserId", serialized)
        self.assertIn("chunkIndex", serialized)
        self.assertIn("sourceVersion", serialized)
        self.assertIn("contentHash", serialized)
        self.assertIn("updatedAt", serialized)
        self.assertNotIn("chunk_id", serialized)

    def test_knowledge_chunk_rejects_unknown_visibility(self):
        with self.assertRaises(ValidationError):
            KnowledgeChunk(
                chunkId="policy:internal:001",
                sourceType="policy",
                sourceId="internal-001",
                content="内部运营备注。",
                visibility="everyone",
            )

    def test_private_chunk_requires_owner(self):
        with self.assertRaisesRegex(ValidationError, "require ownerUserId"):
            KnowledgeChunk(
                chunkId="review:private-001",
                sourceType="review",
                sourceId="private-001",
                content="用户私有笔记。",
                visibility="user_private",
            )

    def test_chunk_rejects_content_hash_that_does_not_match_content(self):
        with self.assertRaisesRegex(ValidationError, "must match content SHA-256"):
            KnowledgeChunk(
                chunkId="policy:demo:001",
                sourceType="policy",
                sourceId="demo",
                content="公开规则。",
                contentHash="0" * 64,
            )

    def test_chunk_can_create_traceable_citation(self):
        chunk = KnowledgeChunk(
            chunk_id="review:review-005",
            source_type="review",
            source_id="review-005",
            title="清晨手冲咖啡评论",
            content="店里有靠窗的单人位和插座，下午写作业或办公很舒服。",
            metadata={"shopId": 3},
        )

        citation = chunk.to_citation(
            score=0.87,
            quote="店里有靠窗的单人位和插座。",
        )

        self.assertIsInstance(citation, Citation)
        self.assertEqual(citation.chunk_id, "review:review-005")
        self.assertEqual(citation.source_id, "review-005")
        self.assertEqual(citation.score, 0.87)
        self.assertEqual(citation.quote, "店里有靠窗的单人位和插座。")
        self.assertEqual(citation.metadata["shopId"], 3)

    def test_retrieval_trace_records_sources_filters_steps_and_citations(self):
        citation = Citation(
            chunkId="review:review-005",
            sourceType="review",
            sourceId="review-005",
            quote="店里有靠窗的单人位和插座。",
            score=0.87,
        )
        trace = RetrievalTrace(
            query="哪家咖啡店适合办公？",
            rewrittenQuery="适合办公 有插座 安静 咖啡店",
            selectedSources=["reviews"],
            filters={"typeId": 2, "visibility": "public"},
            candidateCount=12,
            returnedCount=1,
            durationMs=31.5,
            citations=[citation],
            steps=[
                RetrievalStep(
                    name="router",
                    durationMs=1.2,
                    detail={"selectedSources": ["reviews"]},
                ),
                RetrievalStep(
                    name="vector",
                    durationMs=30.3,
                    detail={"topK": 3},
                ),
            ],
        )

        self.assertEqual(trace.selected_sources, ["reviews"])
        self.assertEqual(trace.filters["typeId"], 2)
        self.assertEqual(trace.steps[1].name, "vector")

        serialized = trace.model_dump(by_alias=True)
        self.assertEqual(serialized["candidateCount"], 12)
        self.assertEqual(serialized["returnedCount"], 1)
        self.assertEqual(serialized["citations"][0]["chunkId"], "review:review-005")
        self.assertEqual(serialized["steps"][0]["durationMs"], 1.2)

    def test_search_knowledge_result_wraps_chunks_citations_and_trace(self):
        chunk = KnowledgeChunk(
            chunkId="merchant_doc:business-001:profile",
            sourceType="merchant_doc",
            sourceId="business-001",
            content="商户资料：WiFi 免费，周一 7:00-20:00。",
        )
        citation = chunk.to_citation(score=0.75)
        trace = RetrievalTrace(
            query="这家店有 WiFi 吗？",
            selectedSources=["merchant_docs"],
            candidateCount=1,
            returnedCount=1,
            citations=[citation],
        )

        result = SearchKnowledgeResult(
            chunks=[chunk],
            citations=[citation],
            trace=trace,
        )

        serialized = result.model_dump(by_alias=True)
        self.assertEqual(serialized["chunks"][0]["sourceType"], "merchant_doc")
        self.assertEqual(serialized["citations"][0]["chunkId"], "merchant_doc:business-001:profile")
        self.assertEqual(serialized["trace"]["selectedSources"], ["merchant_docs"])


if __name__ == "__main__":
    unittest.main()
