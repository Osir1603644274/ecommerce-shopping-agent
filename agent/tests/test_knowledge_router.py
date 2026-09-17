import unittest

from app.knowledge import route_knowledge_sources


class KnowledgeRouterTests(unittest.TestCase):
    def test_routes_policy_questions_to_policy_docs(self):
        self.assertEqual(
            route_knowledge_sources("押金不退怎么办，可以投诉吗？"),
            ["policy_docs"],
        )

    def test_routes_merchant_fact_questions_to_merchant_docs(self):
        self.assertEqual(
            route_knowledge_sources("St Honore Pastries 周末几点关门，有 WiFi 吗？"),
            ["merchant_docs"],
        )

    def test_routes_experience_questions_to_reviews(self):
        self.assertEqual(
            route_knowledge_sources("哪家咖啡店实际比较安静，适合办公？"),
            ["reviews"],
        )

    def test_routes_mixed_merchant_and_experience_questions_to_both_sources(self):
        self.assertEqual(
            route_knowledge_sources("哪家咖啡店有 WiFi，而且实际适合办公？"),
            ["merchant_docs", "reviews"],
        )

    def test_routes_privacy_recommendation_question_to_policy_docs(self):
        self.assertEqual(
            route_knowledge_sources("猜你喜欢为什么要用我的个人信息？"),
            ["policy_docs"],
        )

    def test_defaults_to_reviews_for_ambiguous_local_life_question(self):
        self.assertEqual(
            route_knowledge_sources("推荐一个下午能坐一会儿的地方"),
            ["reviews"],
        )

    def test_empty_query_defaults_to_reviews(self):
        self.assertEqual(route_knowledge_sources("   "), ["reviews"])


if __name__ == "__main__":
    unittest.main()
