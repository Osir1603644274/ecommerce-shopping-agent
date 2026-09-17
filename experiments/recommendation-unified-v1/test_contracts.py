"""Boundary tests for isolated contracts; run with standard-library unittest."""
import unittest
from dataclasses import replace

from contracts import (
    CandidateScope, Interaction, Product, ProductKey, Provenance,
    RecommendationResult, adapt_kuaisearch_document, adapt_kuaisearch_lite_item, build_catalog,
    select_history, validate_recommendation_result,
)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.provenance = Provenance("fixture://explicit-test-data")
        self.a = Product(ProductKey("kuai", "42"), self.provenance, title="Same title")
        self.b = Product(ProductKey("kuai", "43"), self.provenance, title="Same title")
        self.foreign = Product(ProductKey("amazon", "42"), self.provenance, title="Same title")
        self.catalog = build_catalog([self.a, self.b, self.foreign])
        self.scope = CandidateScope("kuai", "catalog-v1", "task-v1")

    def result(self, *keys, scope=None):
        return RecommendationResult(scope or self.scope, keys)

    def event(self, **overrides):
        fields = dict(user_namespace="kuai-users", user_id="u1", product_key=self.a.key,
                      event_kind="click", timestamp=10, timestamp_kind="relative",
                      split="train", provenance=self.provenance)
        fields.update(overrides)
        return Interaction(**fields)

    def test_same_id_from_two_sources_never_collides(self):
        self.assertNotEqual(self.a.key, self.foreign.key)
        self.assertEqual(len(build_catalog([self.a, self.foreign])), 2)
        self.assertIs(self.catalog[self.foreign.key], self.foreign)

    def test_same_title_different_keys_both_survive(self):
        products = validate_recommendation_result(self.result(self.a.key, self.b.key), self.scope, self.catalog)
        self.assertEqual(products, (self.a, self.b))

    def test_catalog_duplicate_identity_is_error(self):
        with self.assertRaisesRegex(ValueError, "duplicate catalog"):
            build_catalog([self.a, replace(self.a, title="different title")])

    def test_missing_price_is_unknown_and_not_zero(self):
        self.assertIsNone(self.a.price_minor)
        self.assertIsNone(self.a.currency)
        self.assertIsNone(self.a.money_unit)
        self.assertEqual(self.a.price_missing_reason, "not_provided")
        with self.assertRaises(ValueError):
            replace(self.a, price_missing_reason=None)

    def test_known_price_requires_explicit_unit_currency_and_evidence(self):
        for changes in ({"price_minor": 100}, {"price_minor": True}, {"price_minor": 1.5}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.a, **changes)
        with self.assertRaisesRegex(ValueError, "source evidence"):
            replace(self.a, price_minor=100, currency="CNY", money_unit="minor", price_missing_reason=None)
        priced = replace(self.a, price_minor=100, currency="CNY", money_unit="minor",
                         price_missing_reason=None,
                         provenance=replace(self.provenance, price_evidence="fixture field price_minor"))
        self.assertEqual(priced.price_minor, 100)

    def test_document_adapter_preserves_raw_attributes_without_inventing_price(self):
        product = adapt_kuaisearch_document(
            {"doc_id": "ksd-test", "title": "商品", "brand": "品牌", "attr_value": "原始,属性",
             "seller_name": "店铺", "price": 1}, source="kuai", source_ref="fixture://documents", row_number=1)
        self.assertEqual(product.key, ProductKey("kuai", "ksd-test"))
        self.assertEqual(product.attributes["attr_value"], "原始,属性")
        self.assertIsNone(product.category)
        self.assertIsNone(product.price_minor)
        self.assertEqual(product.price_missing_reason, "not_in_source_schema")
        self.assertEqual(product.provenance.row_number, 1)

    def test_document_adapter_does_not_alias_raw_item_id(self):
        with self.assertRaisesRegex(ValueError, "doc_id"):
            adapt_kuaisearch_document({"item_id": "42"}, source="kuai", source_ref="fixture://raw")

    def test_lite_adapter_keeps_raw_ids_and_unknown_source_values(self):
        product = adapt_kuaisearch_lite_item(
            {"item_id": 3359347, "item_title": "商品", "brand_name": "UNKNOWN", "brand_id": 0,
             "category_level1_name": "女装", "category_level2_name": "皮草",
             "category_level3_name": "UNKNOWN", "category_level3_id": 0},
            source_ref="fixture://items_lite.train.jsonl")
        self.assertEqual(product.key, ProductKey("kuaisearch", "3359347"))
        self.assertEqual(product.category, "女装 / 皮草")
        self.assertEqual(product.attributes["category_level3_name"], "UNKNOWN")
        self.assertIsNone(product.brand)
        self.assertIsNone(product.price_minor)
        self.assertIsNone(product.currency)
        for invalid_id in (True, 1.25, None, ""):
            with self.subTest(item_id=invalid_id), self.assertRaises(ValueError):
                adapt_kuaisearch_lite_item({"item_id": invalid_id}, source_ref="fixture://items")

    def test_review_is_not_a_click_or_purchase(self):
        review = self.event(event_kind="review")
        history = select_history([review], self.event(timestamp=11, split="test"))
        self.assertEqual([event.event_kind for event in history], ["review"])
        self.assertEqual(sum(event.event_kind == "click" for event in history), 0)
        self.assertEqual(sum(event.event_kind == "purchase" for event in history), 0)

    def test_cross_catalog_result_is_rejected_even_when_id_exists(self):
        with self.assertRaisesRegex(ValueError, "cross-catalog"):
            validate_recommendation_result(self.result(self.foreign.key), self.scope, self.catalog)

    def test_stale_task_or_catalog_result_is_rejected(self):
        for changed in (replace(self.scope, task_revision="task-old"),
                        replace(self.scope, catalog_id="catalog-old")):
            with self.subTest(scope=changed), self.assertRaisesRegex(ValueError, "scope is stale"):
                validate_recommendation_result(self.result(self.a.key, scope=changed), self.scope, self.catalog)

    def test_duplicate_result_and_unknown_id_are_rejected(self):
        for result, pattern in ((self.result(self.a.key, self.a.key), "duplicate result"),
                                (self.result(ProductKey("kuai", "unknown")), "unknown product")):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(ValueError, pattern):
                validate_recommendation_result(result, self.scope, self.catalog)

    def test_candidate_allowlist_is_enforced(self):
        scoped = replace(self.scope, allowed_product_keys=frozenset({self.a.key}))
        with self.assertRaisesRegex(ValueError, "outside candidate scope"):
            validate_recommendation_result(self.result(self.b.key, scope=scoped), scoped, self.catalog)
        empty = replace(self.scope, allowed_product_keys=frozenset())
        with self.assertRaisesRegex(ValueError, "outside candidate scope"):
            validate_recommendation_result(self.result(self.a.key, scope=empty), empty, self.catalog)

    def test_equal_timestamps_and_future_events_are_excluded(self):
        target = self.event(timestamp=10, split="test")
        events = [self.event(timestamp=t) for t in (10, 9, 11, 1)]
        self.assertEqual([event.timestamp for event in select_history(events, target)], [1, 9])

    def test_history_is_user_namespaced_and_train_only_by_default(self):
        events = [self.event(timestamp=1), self.event(timestamp=2, user_namespace="amazon-users"),
                  self.event(timestamp=3, user_id="u2"), self.event(timestamp=4, split="test")]
        self.assertEqual([event.timestamp for event in select_history(events, self.event())], [1])

    def test_mixed_timestamp_units_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "incomparable timestamp"):
            select_history([self.event(timestamp=1, timestamp_kind="unix_seconds")], self.event())

    def test_unknown_event_kind_and_boolean_time_are_rejected(self):
        for overrides in ({"event_kind": "positive"}, {"timestamp": True}, {"timestamp_kind": "unknown"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.event(**overrides)


if __name__ == "__main__":
    unittest.main()
