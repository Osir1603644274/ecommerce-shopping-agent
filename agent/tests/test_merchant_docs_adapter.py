import unittest

from app.knowledge import (
    MERCHANT_DOC_SOURCE_TYPE,
    load_merchant_doc_chunks,
    load_merchant_docs,
    merchant_doc_to_chunk,
)


class MerchantDocsAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = load_merchant_docs()
        cls.chunks = load_merchant_doc_chunks()

    def test_loads_beijing_localized_yelp_merchant_docs(self):
        self.assertEqual(len(self.docs), 300)
        self.assertEqual(self.docs[0]["sourceType"], "merchant_doc")
        self.assertEqual(self.docs[0]["sourceId"], self.docs[0]["sourceBusinessId"])
        self.assertEqual(self.docs[0]["city"], "北京")
        self.assertEqual(self.docs[0]["coordinateSystem"], "BD-09")
        self.assertEqual(self.docs[0]["dataNature"], "localized_demo")
        self.assertTrue(self.docs[0]["content"].strip())

    def test_merchant_doc_to_chunk_preserves_identity_and_metadata(self):
        doc = self.docs[0]

        chunk = merchant_doc_to_chunk(doc)

        self.assertEqual(chunk.source_type, MERCHANT_DOC_SOURCE_TYPE)
        self.assertEqual(chunk.source_id, doc["sourceBusinessId"])
        self.assertEqual(
            chunk.chunk_id,
            f"merchant_doc:{doc['sourceBusinessId']}:profile",
        )
        self.assertEqual(
            chunk.title,
            f"{doc['name']} 商户资料（Yelp原名：{doc['originalName']}）",
        )
        self.assertEqual(chunk.visibility, "public")
        self.assertEqual(chunk.metadata["shopId"], doc["shopId"])
        self.assertEqual(chunk.metadata["typeName"], doc["typeName"])
        self.assertIn("Yelp 来源评分", chunk.content)
        self.assertIn("不代表北京真实登记商家", chunk.content)
        self.assertEqual(chunk.chunk_index, 1)
        self.assertEqual(chunk.source_version, "1")
        self.assertEqual(len(chunk.content_hash), 64)

    def test_all_merchant_docs_convert_to_public_chunks(self):
        self.assertEqual(len(self.chunks), 300)
        self.assertTrue(all(chunk.source_type == MERCHANT_DOC_SOURCE_TYPE for chunk in self.chunks))
        self.assertTrue(all(chunk.visibility == "public" for chunk in self.chunks))
        self.assertTrue(all(chunk.metadata.get("sourceBusinessId") for chunk in self.chunks))

    def test_known_merchant_doc_keeps_real_yelp_attributes_and_hours(self):
        st_honore = next(
            doc
            for doc in self.docs
            if doc["sourceBusinessId"] == "MTSW4McQd7CbVtyjqoe9mw"
        )
        chunk = merchant_doc_to_chunk(st_honore)

        self.assertEqual(st_honore["sourceBusinessId"], "MTSW4McQd7CbVtyjqoe9mw")
        self.assertEqual(st_honore["originalName"], "St Honore Pastries")
        self.assertNotEqual(st_honore["name"], st_honore["originalName"])
        self.assertEqual(st_honore["attributes"]["WiFi"], "free")
        self.assertIn("WiFi：免费", chunk.content)
        self.assertIn("周一 7:00-20:00", chunk.content)
        self.assertIn("Coffee & Tea", chunk.metadata["categories"])


if __name__ == "__main__":
    unittest.main()
