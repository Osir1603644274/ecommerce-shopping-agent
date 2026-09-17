import unittest
from pathlib import Path

from app.knowledge import (
    POLICY_DOC_SOURCE_TYPE,
    load_policy_markdown_chunks,
    parse_markdown_front_matter,
    policy_markdown_to_chunks,
)


class PolicyDocsAdapterTests(unittest.TestCase):
    def test_parse_markdown_front_matter_reads_metadata_and_body(self):
        metadata, body = parse_markdown_front_matter(
            """---
sourceType: policy_doc
sourceId: demo-policy
title: 示例规则
tags: [platform, privacy]
---

# 示例规则

## 第一节

正文。"""
        )

        self.assertEqual(metadata["sourceType"], "policy_doc")
        self.assertEqual(metadata["sourceId"], "demo-policy")
        self.assertEqual(metadata["tags"], ["platform", "privacy"])
        self.assertIn("## 第一节", body)

    def test_policy_markdown_to_chunks_splits_sections(self):
        chunks = policy_markdown_to_chunks(
            """---
sourceType: policy_doc
sourceId: demo-policy
title: 示例规则
sourceTitle: 示例来源
sourceOrg: 示例机构
sourceUrl: https://example.test/policy
sourceVersion: 2026-07-13
updatedAt: 2026-07-13T09:00:00
visibility: public
language: zh
tags: [demo]
---

# 示例规则

## 规则公开 {#rule-disclosure}

平台应当公开重要规则。

## 申诉通道 {#appeal-channel}

平台应当提供申诉通道。"""
        )

        self.assertEqual(len(chunks), 2)
        self.assertEqual(
            chunks[0].chunk_id,
            "policy_doc:demo-policy:rule-disclosure",
        )
        self.assertEqual(chunks[0].source_type, POLICY_DOC_SOURCE_TYPE)
        self.assertEqual(chunks[0].source_id, "demo-policy")
        self.assertEqual(chunks[0].title, "示例规则 - 规则公开")
        self.assertIn("平台应当公开重要规则", chunks[0].content)
        self.assertEqual(chunks[0].metadata["sourceUrl"], "https://example.test/policy")
        self.assertEqual(chunks[1].metadata["sectionIndex"], 2)
        self.assertEqual(chunks[0].metadata["sectionKey"], "rule-disclosure")
        self.assertEqual(
            chunks[0].metadata["legacyChunkId"],
            "policy_doc:demo-policy:001",
        )
        self.assertEqual(chunks[0].tags, ["demo"])
        self.assertEqual(chunks[0].chunk_index, 1)
        self.assertEqual(chunks[1].chunk_index, 2)
        self.assertEqual(chunks[0].source_version, "2026-07-13")
        self.assertEqual(chunks[0].updated_at.isoformat(), "2026-07-13T09:00:00")
        self.assertEqual(len(chunks[0].content_hash), 64)

    def test_policy_markdown_requires_source_id(self):
        with self.assertRaises(ValueError):
            policy_markdown_to_chunks(
                """---
sourceType: policy_doc
title: 缺少编号
---

# 缺少编号

## 规则

正文。"""
            )

    def test_policy_section_requires_stable_key(self):
        with self.assertRaisesRegex(ValueError, "require a stable key"):
            policy_markdown_to_chunks(
                """---
sourceType: policy_doc
sourceId: demo-policy
---

# 示例规则

## 没有稳定编号的章节

正文。"""
            )

    def test_inserting_section_does_not_change_existing_chunk_ids(self):
        original = """---
sourceType: policy_doc
sourceId: demo-policy
---

# 示例规则

## 退款规则 {#refund-rules}

退款正文。"""
        with_inserted_section = original.replace(
            "## 退款规则",
            "## 新增说明 {#new-notice}\n\n新增正文。\n\n## 退款规则",
        )

        original_chunk = policy_markdown_to_chunks(original)[0]
        migrated_chunks = policy_markdown_to_chunks(with_inserted_section)
        migrated_refund = next(
            chunk
            for chunk in migrated_chunks
            if chunk.metadata["sectionKey"] == "refund-rules"
        )

        self.assertEqual(original_chunk.chunk_id, migrated_refund.chunk_id)
        self.assertNotEqual(original_chunk.chunk_index, migrated_refund.chunk_index)

    def test_raw_policy_docs_load_into_public_chunks_with_source_urls(self):
        policy_dir = (
            Path(__file__).resolve().parents[1]
            / "knowledge_data"
            / "raw"
            / "policy_docs"
        )

        chunks = load_policy_markdown_chunks(policy_dir)

        self.assertGreaterEqual(len(chunks), 9)
        source_ids = {chunk.source_id for chunk in chunks}
        self.assertEqual(
            source_ids,
            {
                "platform-rules",
                "consumer-rights",
                "privacy-and-recommendation",
            },
        )
        self.assertTrue(all(chunk.visibility == "public" for chunk in chunks))
        self.assertTrue(all(chunk.metadata.get("sourceUrl") for chunk in chunks))
        self.assertTrue(all(chunk.source_type == POLICY_DOC_SOURCE_TYPE for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
