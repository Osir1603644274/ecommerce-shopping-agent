import unittest

from evaluation.mixed_review_corpus_evaluation import (
    build_mixed_review_corpus_artifacts,
)


def _review(
    review_id: str,
    source: str,
    shop_id: int,
    score: float,
) -> dict:
    return {
        "reviewId": review_id,
        "source": source,
        "shopId": shop_id,
        "shopName": f"shop-{shop_id}",
        "text": f"evidence-{review_id}",
        "score": score,
    }


class MixedReviewCorpusEvaluationTests(unittest.TestCase):
    def test_keeps_strata_separate_and_pools_cross_source_candidates(self):
        seed_cases = [
            {
                "id": "seed-1",
                "question": "seed question",
                "relevantReviewIds": ["seed-r1"],
            }
        ]
        yelp_cases = [
            {
                "id": "yelp-1",
                "question": "yelp question",
                "relevanceJudgments": [
                    {
                        "shopId": 2,
                        "relevance": 3,
                        "supportingReviewIds": ["yelp-r1"],
                    }
                ],
            }
        ]
        rows = {
            "seed question": {
                "legacyVectorAll": [
                    _review("seed-r1", "seed", 1, 0.9),
                    _review("yelp-r1", "yelp", 2, 0.8),
                ],
                "unifiedVectorAll": [
                    _review("seed-r1", "seed", 1, 0.9),
                    _review("yelp-r1", "yelp", 2, 0.8),
                ],
                "bm25YelpOnly": [_review("yelp-r1", "yelp", 2, 1.0)],
                "bm25AllReviews": [
                    _review("seed-r1", "seed", 1, 1.0),
                    _review("yelp-r1", "yelp", 2, 0.5),
                ],
            },
            "yelp question": {
                "legacyVectorAll": [
                    _review("yelp-r1", "yelp", 2, 0.9),
                    _review("seed-r1", "seed", 1, 0.8),
                ],
                "unifiedVectorAll": [
                    _review("yelp-r1", "yelp", 2, 0.9),
                    _review("seed-r1", "seed", 1, 0.8),
                ],
                "bm25YelpOnly": [_review("yelp-r1", "yelp", 2, 1.0)],
                "bm25AllReviews": [
                    _review("yelp-r1", "yelp", 2, 1.0),
                    _review("seed-r1", "seed", 1, 0.5),
                ],
            },
        }

        def retriever(name: str):
            return lambda question, limit: rows[question][name][:limit]

        retrievers = {
            name: retriever(name)
            for name in (
                "legacyVectorAll",
                "unifiedVectorAll",
                "bm25YelpOnly",
                "bm25AllReviews",
            )
        }
        corpus = [
            _review("seed-r1", "seed", 1, 0.0),
            _review("yelp-r1", "yelp", 2, 0.0),
        ]

        report, pool = build_mixed_review_corpus_artifacts(
            seed_cases=seed_cases,
            yelp_cases=yelp_cases,
            reviews=corpus,
            retrievers=retrievers,
            candidate_limit=2,
            judgment_artifact={
                "methodology": {
                    "annotatorType": "assistant_review",
                    "annotatedAt": "2026-07-15",
                },
                "judgments": [
                    {
                        "caseId": "seed-1",
                        "reviewId": "yelp-r1",
                        "relevance": 0,
                        "label": "irrelevant",
                        "rationale": "Does not answer the seed question.",
                    },
                    {
                        "caseId": "yelp-1",
                        "reviewId": "seed-r1",
                        "relevance": 2,
                        "label": "relevant_with_missing_constraint",
                        "rationale": "Useful evidence but misses one constraint.",
                    },
                ],
            },
        )

        self.assertEqual(report["corpusSnapshot"]["reviewCountsBySource"], {"seed": 1, "yelp": 1})
        self.assertIn("seedKnownEvidence", report["strata"])
        self.assertIn("yelpShopDiscovery", report["strata"])
        self.assertFalse(report["decision"]["defaultAgentChanged"])
        self.assertTrue(report["decision"]["hybridCanaryBm25CorpusChanged"])
        self.assertTrue(pool["methodology"]["requiresHumanJudgment"])
        pooled_sources = {
            candidate["source"]
            for case in pool["cases"]
            for candidate in case["crossSourceCandidates"]
        }
        self.assertEqual(pooled_sources, {"seed", "yelp"})
        self.assertTrue(pool["judgmentSummary"]["highPriorityComplete"])
        self.assertEqual(pool["judgmentSummary"]["judgedCountsByRelevance"], {"0": 1, "2": 1})
        self.assertEqual(
            pool["judgmentSummary"]["top3CrossSourceByMethod"]["bm25AllReviews"],
            {
                "candidateCount": 2,
                "countsByRelevance": {"0": 1, "2": 1},
                "irrelevantCaseCount": 1,
                "irrelevantCaseIds": ["seed-1"],
            },
        )
        self.assertEqual(report["candidatePoolSummary"]["judgmentSummary"]["judgedCount"], 2)

    def test_rejects_judgment_that_does_not_match_pool(self):
        with self.assertRaisesRegex(ValueError, "do not match"):
            build_mixed_review_corpus_artifacts(
                seed_cases=[],
                yelp_cases=[],
                reviews=[],
                retrievers={
                    name: (lambda question, limit: [])
                    for name in (
                        "legacyVectorAll",
                        "unifiedVectorAll",
                        "bm25YelpOnly",
                        "bm25AllReviews",
                    )
                },
                judgment_artifact={
                    "judgments": [
                        {
                            "caseId": "missing-case",
                            "reviewId": "missing-review",
                            "relevance": 0,
                            "label": "irrelevant",
                            "rationale": "Unknown candidate.",
                        }
                    ]
                },
            )

    def test_rejects_incomplete_fixed_retriever_set(self):
        with self.assertRaisesRegex(ValueError, "four fixed base methods"):
            build_mixed_review_corpus_artifacts(
                seed_cases=[],
                yelp_cases=[],
                reviews=[],
                retrievers={},
            )


if __name__ == "__main__":
    unittest.main()
