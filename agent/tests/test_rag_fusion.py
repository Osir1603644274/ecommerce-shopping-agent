import unittest

from app.rag_fusion import fuse_by_normalized_score, fuse_by_rrf


def result(review_id, score):
    return {
        "reviewId": review_id,
        "shopId": 1,
        "text": review_id,
        "score": score,
    }


class RagFusionTests(unittest.TestCase):
    def test_normalized_score_fusion_uses_union_and_preserves_both_scores(self):
        fused = fuse_by_normalized_score(
            [result("a", 0.9), result("b", 0.7)],
            [result("b", 12.0), result("c", 4.0)],
        )

        self.assertEqual({item["reviewId"] for item in fused}, {"a", "b", "c"})
        by_id = {item["reviewId"]: item for item in fused}
        self.assertEqual(by_id["a"]["vectorRank"], 1)
        self.assertIsNone(by_id["a"]["bm25Rank"])
        self.assertEqual(by_id["b"]["vectorScore"], 0.7)
        self.assertEqual(by_id["b"]["bm25Score"], 12.0)
        self.assertEqual(by_id["b"]["normalizedBm25Score"], 1.0)
        self.assertLessEqual(max(item["normalizedVectorScore"] for item in fused), 1.0)

    def test_rrf_rewards_documents_found_by_both_retrievers(self):
        fused = fuse_by_rrf(
            [result("a", 0.9), result("b", 0.8), result("c", 0.7)],
            [result("c", 10.0), result("a", 8.0), result("d", 7.0)],
        )

        self.assertEqual([item["reviewId"] for item in fused[:2]], ["a", "c"])
        self.assertGreater(fused[1]["rrfScore"], fused[2]["rrfScore"])

    def test_equal_raw_scores_normalize_to_full_presence_credit(self):
        fused = fuse_by_normalized_score(
            [result("a", 0.5), result("b", 0.5)],
            [],
        )

        self.assertTrue(
            all(item["normalizedVectorScore"] == 1.0 for item in fused)
        )

    def test_max_normalization_preserves_lowest_result_contribution(self):
        fused = fuse_by_normalized_score(
            [result("a", 0.8), result("b", 0.4)],
            [],
            normalization="max",
        )

        by_id = {item["reviewId"]: item for item in fused}
        self.assertEqual(by_id["a"]["normalizedVectorScore"], 1.0)
        self.assertEqual(by_id["b"]["normalizedVectorScore"], 0.5)

    def test_rejects_invalid_fusion_parameters(self):
        with self.assertRaisesRegex(ValueError, "non-negative"):
            fuse_by_normalized_score([], [], vector_weight=-1)
        with self.assertRaisesRegex(ValueError, "at least one"):
            fuse_by_normalized_score([], [], vector_weight=0, bm25_weight=0)
        with self.assertRaisesRegex(ValueError, "rrf_k must be positive"):
            fuse_by_rrf([], [], rrf_k=0)
        with self.assertRaisesRegex(ValueError, "normalization must be"):
            fuse_by_normalized_score([], [], normalization="unknown")


if __name__ == "__main__":
    unittest.main()
