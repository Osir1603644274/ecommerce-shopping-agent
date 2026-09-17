import unittest

from evaluation.rag_dual_recall_evaluation import (
    build_dual_recall_report,
    build_yelp_corpus_snapshot,
)


def item(review_id, score):
    return {"reviewId": review_id, "score": score, "text": review_id}


class RagDualRecallEvaluationTests(unittest.TestCase):
    def test_compares_independent_retrievers_and_candidate_union(self):
        cases = [
            {
                "id": "case-1",
                "question": "query",
                "relevantReviewIds": ["bm25-only"],
            }
        ]

        def vector_retrieve(question, limit):
            self.assertEqual(limit, 2)
            return [item("vector-only", 0.9), item("shared", 0.8)]

        def bm25_retrieve(question, limit):
            return [item("bm25-only", 9.0), item("shared", 3.0)]

        report = build_dual_recall_report(
            cases,
            vector_retrieve=vector_retrieve,
            bm25_retrieve=bm25_retrieve,
            candidate_limit=2,
            top_k=2,
        )

        self.assertEqual(report["candidateUnion"]["hits"], 1)
        self.assertEqual(report["candidateUnion"]["averageOverlapSize"], 1.0)
        self.assertEqual(report["candidateUnion"]["averageUnionSize"], 3.0)
        self.assertEqual(
            report["methodology"]["frozenWeightedFusion"],
            {"vector": 0.2, "bm25": 0.8, "selectionUse": False},
        )
        self.assertEqual(report["methods"]["vector"]["metrics"]["hits"], 0)
        self.assertEqual(report["methods"]["bm25"]["metrics"]["hits"], 1)
        self.assertEqual(report["methods"]["minMaxFusion"]["metrics"]["hits"], 1)
        self.assertEqual(
            report["methods"]["minMaxFusionV02B08"]["metrics"]["hits"],
            1,
        )
        self.assertEqual(report["methods"]["maxFusion"]["metrics"]["hits"], 1)
        self.assertEqual(report["methods"]["rrf"]["metrics"]["hits"], 1)

    def test_corpus_snapshot_is_deterministic_and_tracks_indexed_text(self):
        reviews = [
            {
                "reviewId": "b",
                "source": "yelp",
                "text": "第二条",
                "language": "zh",
                "translationStatus": "translated",
            },
            {
                "reviewId": "a",
                "source": "yelp",
                "text": "第一条",
                "language": "zh",
                "translationStatus": "translated",
            },
            {"reviewId": "seed", "source": "seed", "text": "忽略"},
        ]

        snapshot = build_yelp_corpus_snapshot(reviews)
        reversed_snapshot = build_yelp_corpus_snapshot(list(reversed(reviews)))

        self.assertEqual(snapshot, reversed_snapshot)
        self.assertEqual(snapshot["reviewCount"], 2)
        self.assertEqual(snapshot["translatedCount"], 2)
        self.assertEqual(snapshot["indexedLanguageCounts"], {"zh": 2})
        self.assertEqual(len(snapshot["retrievalTextSha256"]), 64)


if __name__ == "__main__":
    unittest.main()
