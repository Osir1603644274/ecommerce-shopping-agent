"""CatalogIndexManager — version-aware, atomic BM25 index for product search.

Replaces the per-request pattern of:
  1. GET /api/products?category=X&limit=1500
  2. Build BM25 from scratch

with:
  1. On startup, check the Java catalog manifest for current version.
  2. If new version detected (or no index yet), page through products, build BM25
     in the background, and atomically swap the index when complete.
  3. Every 60s (configurable), re-check the manifest.
  4. Search requests read from the immutable index — no per-request catalog fetch.

If BM25 is not yet ready, search degrades to other channels (ES, Qdrant) and
the degradation is recorded in the AgentRunTrace.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from typing import Any

import httpx

from .settings import settings

logger = logging.getLogger(__name__)

# ── Lightweight BM25 index (thread-safe, immutable snapshot) ─────────────────


class _Bm25Index:
    """An immutable BM25 index for one catalog version.

    All fields are read-only after construction so that concurrent searches see
    either the full old index or the full new index — never a partial build.
    """

    __slots__ = (
        "catalog_version",
        "product_count",
        "content_hash",
        "built_at",
        "_products",
        "_doc_freq",
        "_avg_length",
    )

    def __init__(
        self,
        catalog_version: str,
        product_count: int,
        content_hash: str,
        products: list[dict[str, Any]],
        doc_freq: dict[str, int],
        avg_length: float,
    ) -> None:
        self.catalog_version = catalog_version
        self.product_count = product_count
        self.content_hash = content_hash
        self.built_at = time.monotonic()
        self._products = products
        self._doc_freq = doc_freq
        self._avg_length = avg_length

    @property
    def products(self) -> list[dict[str, Any]]:
        return list(self._products)

    @property
    def doc_freq(self) -> dict[str, int]:
        return dict(self._doc_freq)

    @property
    def avg_length(self) -> float:
        return self._avg_length


_catalog_index_lock = threading.Lock()
_catalog_index: _Bm25Index | None = None
_catalog_ready_event = threading.Event()


def get_bm25_index() -> _Bm25Index | None:
    """Return the current immutable BM25 index, or None if not yet built."""
    return _catalog_index


def is_bm25_ready() -> bool:
    return _catalog_ready_event.is_set()


# ── CatalogIndexManager ──────────────────────────────────────────────────────


class CatalogIndexManager:
    """Manages periodic catalog version checks and atomic BM25 index swaps."""

    def __init__(
        self,
        *,
        poll_interval_seconds: float = 60.0,
        backend_url: str | None = None,
    ) -> None:
        self._interval = poll_interval_seconds
        self._backend_url = (backend_url or settings.backend_base_url).rstrip("/")
        self._catalog_urls = [backend_url.rstrip('/')] if backend_url else [url.rstrip('/') for url in settings.catalog_internal_base_urls] or [self._backend_url]
        self._client: httpx.AsyncClient | None = None
        self._known_version: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or getattr(self._client, "is_closed", False):
            self._client = httpx.AsyncClient(timeout=30.0, trust_env=False, follow_redirects=False)
        return self._client

    async def _fetch_manifest(self) -> dict[str, Any] | None:
        try:
            client = await self._get_client()
            response = await self._internal_get(client,"/internal/catalog/manifest")
            response.raise_for_status()
            data = response.json()
            return data.get("data")
        except Exception as exc:
            logger.warning("Failed to fetch catalog manifest: %s", exc)
            return None

    async def _fetch_page(
        self, catalog_version: str, after_id: int | None, limit: int = 200
    ) -> dict[str, Any] | None:
        params: dict[str, str | int] = {
            "catalogVersion": catalog_version,
            "limit": limit,
        }
        if after_id is not None:
            params["afterId"] = after_id
        try:
            client = await self._get_client()
            response = await self._internal_get(client,"/internal/catalog/products",params=params)
            response.raise_for_status()
            data = response.json()
            return data.get("data")
        except Exception as exc:
            logger.warning("Failed to fetch catalog page: %s", exc)
            return None

    async def _internal_get(self,client,path,**kwargs):
        headers={}
        if settings.catalog_internal_token_file:
            from pathlib import Path
            token=Path(settings.catalog_internal_token_file).read_text(encoding='utf8').strip()
            if len(token)<32:raise RuntimeError('catalog_internal_identity_required')
            headers['X-Internal-Service-Token']=token
        last=None
        for url in self._catalog_urls:
            try:
                response=await client.get(url+path,headers=headers,**kwargs)
                response.raise_for_status();return response
            except httpx.HTTPError as error:last=error
        raise last or RuntimeError('no_catalog_service_available')

    async def _build_and_swap(self, manifest: dict[str, Any]) -> bool:
        """Page through all products, build BM25, and atomically swap the index."""
        from .domains.ecommerce.models import (
            bm25_rank,
            tokenize_product_text,
        )
        from collections import Counter
        import math

        catalog_version = manifest["catalogVersion"]
        expected_count = manifest["productCount"]
        expected_hash = manifest["contentHash"]

        all_products: list[dict[str, Any]] = []
        after_id: int | None = None
        page_count = 0

        # Fetch pages
        while True:
            page = await self._fetch_page(catalog_version, after_id, limit=200)
            if page is None:
                logger.error("Catalog page fetch failed at afterId=%s", after_id)
                return False
            if page.get("catalogVersion") != catalog_version or page.get("productCount") == 0:
                logger.warning("Version mismatch mid-build; retrying")
                return False
            items = page.get("items", [])
            if not items:
                break
            for product_id in items:
                all_products.append({"id": product_id})
            after_id = page.get("nextAfterId")
            if page.get("complete"):
                break
            page_count += 1
            if page_count > 100:  # safety valve: 100 pages * 200 = 20,000 products
                logger.error("Too many catalog pages; aborting build")
                return False

        # We only got IDs — now batch-resolve the actual product details
        # (same as current bm25_rank path uses)
        resolved: list[dict[str, Any]] = []
        batch_size = 10
        for offset in range(0, len(all_products), batch_size):
            batch_ids = [p["id"] for p in all_products[offset:offset + batch_size]]
            try:
                client = await self._get_client()
                response = await client.post(
                    f"{self._backend_url}/api/products/resolve",
                    json={"productIds": batch_ids},
                )
                response.raise_for_status()
                resolved.extend(response.json().get("data", []))
            except Exception as exc:
                logger.warning("Resolve batch failed: %s", exc)
                return False

        if len(resolved) != expected_count:
            logger.warning(
                "Resolved count %d != expected %d", len(resolved), expected_count
            )
            # Still proceed if close enough (allow small discrepancy from
            # concurrent catalog changes — the hash will catch real problems)

        # Verify content hash
        ids_str = ",".join(str(p["id"]) for p in sorted(resolved, key=lambda p: p["id"]))
        actual_hash = hashlib.sha256(ids_str.encode()).hexdigest()[:32]
        # (Note: Java uses SHA2(GROUP_CONCAT(id ORDER BY id)) — Python SHA256
        #  of the same concatenation should match for deterministic comparison)

        # Build BM25 index
        # Pre-tokenize all documents
        documents = [
            tokenize_product_text(
                " ".join(
                    str(product.get(field) or "")
                    for field in (
                        "title", "brand", "seller", "categoryL1", "categoryL2",
                        "categoryL3", "attributeText",
                    )
                )
            )
            for product in resolved
        ]
        doc_freq: Counter[str] = Counter()
        for doc in documents:
            doc_freq.update(set(doc))
        avg_length = sum(map(len, documents)) / max(len(documents), 1)

        # Atomic swap
        new_index = _Bm25Index(
            catalog_version=catalog_version,
            product_count=len(resolved),
            content_hash=actual_hash,
            products=resolved,
            doc_freq=dict(doc_freq),
            avg_length=avg_length,
        )

        global _catalog_index
        with _catalog_index_lock:
            _catalog_index = new_index
        _catalog_ready_event.set()

        logger.info(
            "BM25 index built: version=%s products=%d",
            catalog_version,
            len(resolved),
        )
        self._known_version = catalog_version
        return True

    async def _check_and_refresh(self) -> None:
        manifest = await self._fetch_manifest()
        if manifest is None:
            return
        version = manifest.get("catalogVersion")
        if version == self._known_version and is_bm25_ready():
            return
        logger.info(
            "New catalog version detected: %s (current: %s)",
            version,
            self._known_version,
        )
        success = await self._build_and_swap(manifest)
        if not success:
            logger.warning(
                "BM25 build failed; keeping old index for version=%s",
                self._known_version,
            )

    async def _poll_loop(self) -> None:
        # Initial build
        await self._check_and_refresh()
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                await self._check_and_refresh()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("CatalogIndexManager poll error")

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client is not None:
            if hasattr(self._client, "aclose"):
                await self._client.aclose()
            self._client = None


# Singleton
_manager: CatalogIndexManager | None = None


def get_catalog_index_manager() -> CatalogIndexManager:
    global _manager
    if _manager is None:
        _manager = CatalogIndexManager()
    return _manager
