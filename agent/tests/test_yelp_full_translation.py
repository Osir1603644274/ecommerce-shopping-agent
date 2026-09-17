import tempfile
import unittest
from pathlib import Path

from recommendation.yelp_full_translation import (
    build_translation_batches,
    build_translation_update_sql,
    cached_translation,
    checkpoint_record,
    load_checkpoint,
    parse_translation_response,
    validate_translation,
)


def review(review_id="yelp-1", content="The coffee was good and service was fast."):
    return {"reviewId": review_id, "content": content}


class YelpFullTranslationTests(unittest.TestCase):
    def test_build_batches_respects_item_and_character_limits(self):
        reviews = [
            review("1", "a" * 4),
            review("2", "b" * 4),
            review("3", "c" * 4),
        ]

        batches = build_translation_batches(reviews, max_chars=7, max_items=2)

        self.assertEqual([[item["reviewId"] for item in batch] for batch in batches], [["1"], ["2"], ["3"]])

    def test_parse_response_requires_exact_ids_and_chinese_text(self):
        parsed = parse_translation_response(
            '{"translations":[{"id":"yelp-1","textZh":"咖啡很好喝，服务也很快。"}]}',
            [review()],
        )
        self.assertEqual(parsed["yelp-1"], "咖啡很好喝，服务也很快。")

        with self.assertRaisesRegex(ValueError, "enough Chinese"):
            parse_translation_response(
                '{"translations":[{"id":"yelp-1","textZh":"coffee good"}]}',
                [review()],
            )

    def test_update_sql_uses_utf8_hex_instead_of_interpolated_text(self):
        sql = build_translation_update_sql({"yelp-1": "店员说：'欢迎'。"})

        self.assertIn("CONVERT(0x", sql)
        self.assertNotIn("店员说", sql)
        self.assertIn("translation_status='translated'", sql)

    def test_bilingual_duplicate_allows_lower_length_ratio(self):
        source = ("Texto repetido. " * 20) + "\n\nEnglish:\n" + ("Repeated text. " * 20)
        translated = "这是同一内容的忠实中文翻译。" * 8

        self.assertEqual(validate_translation(source, translated), translated)

    def test_long_url_is_excluded_from_chinese_ratio_check(self):
        source = "https://example.com/" + ("very-long-path/" * 20) + "\nDo not go here. Disgusting."
        translated = "https://example.com/" + ("very-long-path/" * 20) + "\n不要去这里。很恶心。"

        self.assertEqual(validate_translation(source, translated), translated)

    def test_rejects_a_full_untranslated_duplicate_paragraph(self):
        source = "这家店很好。\nThe restaurant is very good and the service is friendly."
        translated = "这家店很好。\nThe restaurant is very good and the service is friendly."

        with self.assertRaisesRegex(ValueError, "too much untranslated"):
            validate_translation(source, translated)

    def test_checkpoint_is_reused_only_for_same_source_content(self):
        source = review()
        record = checkpoint_record(source, "咖啡很好喝，服务也很快。", model="model")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.jsonl"
            path.write_text(__import__("json").dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
            loaded = load_checkpoint(path)

        self.assertEqual(cached_translation(source, loaded), "咖啡很好喝，服务也很快。")
        changed = review(content="The coffee was bad.")
        self.assertIsNone(cached_translation(changed, loaded))


if __name__ == "__main__":
    unittest.main()
