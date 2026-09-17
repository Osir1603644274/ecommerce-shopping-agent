"""Tests for CatalogIndexManager — BM25 atomic index swap, poll loop, resilience.

These tests verify the CatalogIndexManager's lifecycle, not the BM25 ranking
logic itself (which is tested via bm25_rank in test_reranker_strategy).
"""

import asyncio as aio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.catalog_index import (
    _Bm25Index,
    CatalogIndexManager,
    get_bm25_index,
    is_bm25_ready,
)


def _make_product(product_id, **overrides):
    """Minimal product dict matching Java /internal/catalog/products response."""
    return {
        "id": product_id,
        "title": overrides.get("title", f"Test Product {product_id}"),
        "brand": overrides.get("brand", "TestBrand"),
        "categoryL1": "手机/数码/电脑办公",
        "categoryL2": "手机通讯",
        "categoryL3": "智能手机",
        "attributes": [],
        "source": "snapshot",
        **{k: v for k, v in overrides.items() if k not in ("title", "brand")},
    }


# ── _Bm25Index unit tests ────────────────────────────────────────────────────


class TestBm25Index:
    def test_construction_and_readback(self):
        products = [_make_product(1), _make_product(2)]
        idx = _Bm25Index(
            catalog_version="v1",
            product_count=2,
            content_hash="sha256:abc",
            products=products,
            doc_freq={"phone": 2, "256gb": 1},
            avg_length=12.5,
        )
        assert idx.catalog_version == "v1"
        assert idx.product_count == 2
        assert idx.content_hash == "sha256:abc"
        assert len(idx.products) == 2
        assert idx.doc_freq == {"phone": 2, "256gb": 1}
        assert idx.avg_length == 12.5
        assert idx.built_at > 0

    def test_slots_prevent_new_attributes(self):
        idx = _Bm25Index(
            catalog_version="v1",
            product_count=1,
            content_hash="abc",
            products=[_make_product(1)],
            doc_freq={},
            avg_length=0.0,
        )
        # __slots__ prevents adding arbitrary new attributes
        with pytest.raises(AttributeError):
            idx.extra_field = "should fail"

    def test_products_returns_copy(self):
        original = [_make_product(1)]
        idx = _Bm25Index(
            catalog_version="v1",
            product_count=1,
            content_hash="abc",
            products=original,
            doc_freq={},
            avg_length=0.0,
        )
        copy1 = idx.products
        copy1.append(_make_product(2))
        # Mutating the returned list does NOT mutate the internal store
        assert len(idx.products) == 1

    def test_doc_freq_returns_copy(self):
        idx = _Bm25Index(
            catalog_version="v1",
            product_count=1,
            content_hash="abc",
            products=[_make_product(1)],
            doc_freq={"a": 1},
            avg_length=0.0,
        )
        freq_copy = idx.doc_freq
        freq_copy["b"] = 2
        assert "b" not in idx.doc_freq


# ── CatalogIndexManager lifecycle tests ───────────────────────────────────────


class TestCatalogIndexManager:
    def test_initial_state_not_ready(self):
        import app.catalog_index as mod
        mod._catalog_index = None
        mod._catalog_ready_event = threading.Event()
        # Event must be cleared
        mod._catalog_ready_event.clear()
        assert is_bm25_ready() is False
        assert get_bm25_index() is None

    def test_build_and_swap_sets_ready(self):
        import app.catalog_index as mod
        mod._catalog_index = None
        mod._catalog_ready_event = threading.Event()
        mod._catalog_ready_event.clear()

        mgr = CatalogIndexManager(
            poll_interval_seconds=3600,
            backend_url="http://fake:8080",
        )

        async def _test():
            # Mock _fetch_page and resolve to return product data
            products = [_make_product(i) for i in range(5)]

            async def fake_fetch_page(catalog_version, after_id, limit=200):
                if after_id is not None and after_id >= 10:
                    return {"catalogVersion": catalog_version, "productCount": 0, "items": [], "complete": True}
                item_ids = [p["id"] for p in products]
                next_id = item_ids[-1] if item_ids else None
                return {
                    "catalogVersion": catalog_version,
                    "productCount": len(products),
                    "items": item_ids,
                    "nextAfterId": next_id,
                    "complete": True,
                }

            async def fake_resolve(*args, **kwargs):
                return MagicMock(status_code=200, json=MagicMock(return_value={"data": products}))

            mgr._fetch_page = fake_fetch_page
            with patch.object(mgr, "_get_client") as mock_client_get:
                mock_client = MagicMock()
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json = MagicMock(return_value={"data": products})
                mock_resp.raise_for_status = MagicMock()

                async def fake_post(url, **kw):
                    resp = MagicMock()
                    resp.status_code = 200
                    resp.json = MagicMock(return_value={"data": products})
                    resp.raise_for_status = MagicMock()
                    return resp

                mock_client.post = fake_post
                mock_client_get.return_value = mock_client

                manifest = {
                    "catalogVersion": "v1",
                    "productCount": 5,
                    "contentHash": "sha256:abc",
                }
                success = await mgr._build_and_swap(manifest)
                assert success is True
                assert is_bm25_ready() is True
                idx = get_bm25_index()
                assert idx is not None
                assert idx.catalog_version == "v1"
                assert idx.product_count == 5

        aio.run(_test())

    def test_no_rebuild_when_version_unchanged(self):
        import app.catalog_index as mod
        mod._catalog_index = None
        mod._catalog_ready_event = threading.Event()
        mod._catalog_ready_event.clear()

        mgr = CatalogIndexManager(
            poll_interval_seconds=3600,
            backend_url="http://fake:8080",
        )

        async def _test():
            # Set up a known version
            mgr._known_version = "v1"
            mod._catalog_ready_event.set()
            # Mock _fetch_manifest to return the same version
            mgr._fetch_manifest = AsyncMock(return_value={
                "catalogVersion": "v1",
                "productCount": 5,
                "contentHash": "sha256:abc",
            })
            await mgr._check_and_refresh()
            # Should skip build — version unchanged and ready
            assert mgr._known_version == "v1"

        aio.run(_test())

    def test_version_change_triggers_rebuild(self):
        import app.catalog_index as mod
        mod._catalog_index = None
        mod._catalog_ready_event = threading.Event()
        mod._catalog_ready_event.clear()

        mgr = CatalogIndexManager(
            poll_interval_seconds=3600,
            backend_url="http://fake:8080",
        )
        mgr._known_version = "v1"

        async def _test():
            products = [_make_product(1), _make_product(2)]

            async def fake_fetch_page(catalog_version, after_id, limit=200):
                if after_id is not None and after_id >= 10:
                    return {"catalogVersion": catalog_version, "productCount": 0, "items": [], "complete": True}
                return {
                    "catalogVersion": catalog_version,
                    "productCount": 2,
                    "items": [1, 2],
                    "nextAfterId": None,
                    "complete": True,
                }

            mgr._fetch_page = fake_fetch_page
            # Mock manifest returning new version
            mgr._fetch_manifest = AsyncMock(return_value={
                "catalogVersion": "v2",
                "productCount": 2,
                "contentHash": "sha256:def",
            })

            with patch.object(mgr, "_get_client") as mock_client_get:
                mock_client = MagicMock()
                async def fake_post(url, **kw):
                    resp = MagicMock()
                    resp.status_code = 200
                    resp.json = MagicMock(return_value={"data": products})
                    resp.raise_for_status = MagicMock()
                    return resp
                mock_client.post = fake_post
                mock_client_get.return_value = mock_client

                await mgr._check_and_refresh()
                assert mgr._known_version == "v2"

        aio.run(_test())

    def test_build_failure_retains_old_state(self):
        import app.catalog_index as mod
        mod._catalog_index = None
        mod._catalog_ready_event = threading.Event()
        mod._catalog_ready_event.clear()

        mgr = CatalogIndexManager(
            poll_interval_seconds=3600,
            backend_url="http://fake:8080",
        )

        async def _test():
            # Set up initial good state
            products = [_make_product(1)]
            async def fake_fetch_page(catalog_version, after_id, limit=200):
                if after_id is not None:
                    return {"catalogVersion": catalog_version, "productCount": 0, "items": [], "complete": True}
                return {
                    "catalogVersion": catalog_version,
                    "productCount": 1,
                    "items": [1],
                    "nextAfterId": None,
                    "complete": True,
                }

            mgr._fetch_page = fake_fetch_page
            with patch.object(mgr, "_get_client") as mock_client_get:
                mock_client = MagicMock()
                async def fake_post(url, **kw):
                    resp = MagicMock()
                    resp.status_code = 200
                    resp.json = MagicMock(return_value={"data": products})
                    resp.raise_for_status = MagicMock()
                    return resp
                mock_client.post = fake_post
                mock_client_get.return_value = mock_client

                manifest = {"catalogVersion": "v1", "productCount": 1, "contentHash": "abc"}
                await mgr._build_and_swap(manifest)

            # Now simulate a failed build
            mgr._fetch_page = AsyncMock(return_value=None)
            manifest2 = {"catalogVersion": "v2", "productCount": 999, "contentHash": "def"}
            success = await mgr._build_and_swap(manifest2)
            assert success is False
            # Old index must still be available
            idx = get_bm25_index()
            assert idx is not None
            assert idx.catalog_version == "v1"

        aio.run(_test())


# ── Fetch pipeline tests (mock HTTP) ─────────────────────────────────────────


class TestFetchPipeline:
    def test_fetch_manifest_parses_wrapped_response(self):
        mgr = CatalogIndexManager(
            poll_interval_seconds=3600,
            backend_url="http://fake:8080",
        )

        async def _test():
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json = MagicMock(return_value={
                "data": {
                    "catalogVersion": "v1",
                    "productCount": 150,
                    "contentHash": "sha256:abc",
                    "publishedAt": "2026-01-01T00:00:00Z",
                }
            })
            mock_resp.raise_for_status = MagicMock()
            mock_client.get = AsyncMock(return_value=mock_resp)

            with patch.object(mgr, "_get_client", AsyncMock(return_value=mock_client)):
                manifest = await mgr._fetch_manifest()
            assert manifest is not None
            assert manifest["catalogVersion"] == "v1"
            assert manifest["productCount"] == 150

        aio.run(_test())

    def test_fetch_manifest_handles_error(self):
        mgr = CatalogIndexManager(
            poll_interval_seconds=3600,
            backend_url="http://fake:8080",
        )

        async def _test():
            mock_client = MagicMock()
            mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))

            with patch.object(mgr, "_get_client", AsyncMock(return_value=mock_client)):
                manifest = await mgr._fetch_manifest()
            assert manifest is None

        aio.run(_test())

    def test_fetch_page_handles_version_mismatch(self):
        mgr = CatalogIndexManager(
            poll_interval_seconds=3600,
            backend_url="http://fake:8080",
        )

        async def _test():
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_resp.raise_for_status = MagicMock(
                side_effect=Exception("HTTP 404")
            )
            mock_client.get = AsyncMock(return_value=mock_resp)

            with patch.object(mgr, "_get_client", AsyncMock(return_value=mock_client)):
                page = await mgr._fetch_page("v99-missing", None, limit=100)
            assert page is None

        aio.run(_test())

    def test_fetch_page_returns_empty_when_http_error(self):
        mgr = CatalogIndexManager(
            poll_interval_seconds=3600,
            backend_url="http://fake:8080",
        )

        async def _test():
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 503
            mock_resp.raise_for_status = MagicMock(
                side_effect=Exception("HTTP 503")
            )
            mock_client.get = AsyncMock(return_value=mock_resp)

            with patch.object(mgr, "_get_client", AsyncMock(return_value=mock_client)):
                page = await mgr._fetch_page("v1", 0, limit=100)
            assert page is None

        aio.run(_test())


# ── Concurrent access tests ──────────────────────────────────────────────────


class TestConcurrentAccess:
    def test_concurrent_reads_see_consistent_index(self):
        """Multiple threads reading get the same index object."""
        import app.catalog_index as mod
        mod._catalog_index = None
        mod._catalog_ready_event = threading.Event()
        mod._catalog_ready_event.clear()

        products = [_make_product(i) for i in range(100)]
        idx = _Bm25Index(
            catalog_version="v1",
            product_count=100,
            content_hash="abc",
            products=products,
            doc_freq={"test": 100},
            avg_length=10.0,
        )

        with mod._catalog_index_lock:
            mod._catalog_index = idx
        mod._catalog_ready_event.set()

        results = []

        def read_index():
            for _ in range(50):
                i = get_bm25_index()
                results.append(i is idx)

        threads = [threading.Thread(target=read_index) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(results)  # Every read got the same object
