import unittest

from app.bm25 import BM25Document, BM25Index
from app.rag_bm25_benchmark import (
    build_quality_grid,
    candidate_recall_report,
    prepare_candidate_features,
    select_quality_leaders,
    summarize_durations,
)


class RagBm25BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.cases = [
            {
                "id": "case-1",
                "question": "wifi",
                "relevantReviewIds": ["r2"],
            }
        ]
        self.candidates = {
            "case-1": [
                {"reviewId": "r1", "score": 0.9},
                {"reviewId": "r2", "score": 0.8},
            ]
        }
        self.index = BM25Index(
            [
                BM25Document("r1", "coffee", {"reviewId": "r1"}),
                BM25Document("r2", "wifi wifi", {"reviewId": "r2"}),
            ]
        )

    def test_candidate_recall_changes_with_candidate_limit(self):
        top_1 = candidate_recall_report(
            self.cases,
            self.candidates,
            candidate_limit=1,
        )
        top_2 = candidate_recall_report(
            self.cases,
            self.candidates,
            candidate_limit=2,
        )

        self.assertEqual(top_1["hits"], 0)
        self.assertEqual(top_1["missedCaseIds"], ["case-1"])
        self.assertEqual(top_2["hits"], 1)

    def test_larger_bm25_weight_can_promote_relevant_candidate(self):
        prepared = {
            "case-1": prepare_candidate_features(
                "wifi",
                self.candidates["case-1"],
                self.index,
            )
        }
        grid = build_quality_grid(
            self.cases,
            prepared,
            candidate_limits=[2],
            bm25_weights=[0.0, 10.0],
            top_k=1,
        )

        self.assertEqual(grid[0]["hits"], 0)
        self.assertEqual(grid[1]["hits"], 1)
        self.assertEqual(grid[1]["hitsAt1"], 1)

    def test_quality_leader_reports_ties_and_prefers_smaller_config(self):
        grid = [
            {
                "candidateLimit": 20,
                "bm25Weight": 1.0,
                "hits": 2,
                "mrr": 0.75,
                "hitsAt1": 1,
            },
            {
                "candidateLimit": 10,
                "bm25Weight": 0.2,
                "hits": 2,
                "mrr": 0.75,
                "hitsAt1": 1,
            },
        ]

        leaders = select_quality_leaders(grid)

        self.assertEqual(leaders["tieCount"], 2)
        self.assertEqual(leaders["selected"]["candidateLimit"], 10)
        self.assertEqual(leaders["selected"]["bm25Weight"], 0.2)

    def test_quality_leader_uses_mrr_before_hits_at_1(self):
        grid = [
            {
                "candidateLimit": 10,
                "bm25Weight": 0.2,
                "hits": 2,
                "mrr": 0.70,
                "hitsAt1": 2,
            },
            {
                "candidateLimit": 20,
                "bm25Weight": 1.0,
                "hits": 2,
                "mrr": 0.75,
                "hitsAt1": 1,
            },
        ]

        leaders = select_quality_leaders(grid)

        self.assertEqual(leaders["selected"]["candidateLimit"], 20)
        self.assertEqual(leaders["selected"]["bm25Weight"], 1.0)

    def test_summarizes_nearest_rank_percentiles(self):
        summary = summarize_durations([10.0, 20.0, 30.0, 40.0])

        self.assertEqual(summary["requestCount"], 4)
        self.assertEqual(summary["avgMs"], 25.0)
        self.assertEqual(summary["p50Ms"], 20.0)
        self.assertEqual(summary["p95Ms"], 40.0)


if __name__ == "__main__":
    unittest.main()
