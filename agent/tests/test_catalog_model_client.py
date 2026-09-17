import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock
import pytest
from app import catalog_model_client as pool


def test_connection_is_reused_without_caching_calls_and_closed(monkeypatch):
    from app import llm
    context=AsyncMock();client=SimpleNamespace(create=AsyncMock(side_effect=['first','second']))
    context.__aenter__.return_value=client
    factory=MagicMock(return_value=context)
    monkeypatch.setattr(llm,'get_client',factory)
    monkeypatch.setattr(pool.settings,'catalog_workspace_reuse_model_client',True)
    async def run():
        try:
            async with pool.borrow_client() as c:assert await c.create('first input')=='first'
            async with pool.borrow_client() as c:assert await c.create('second input')=='second'
            assert factory.call_count==1 and client.create.await_count==2
        finally:await pool.close_client()
        context.__aexit__.assert_awaited_once()
    asyncio.run(run())


def test_failed_borrower_does_not_destroy_shared_pool(monkeypatch):
    from app import llm
    context=AsyncMock();factory=MagicMock(return_value=context)
    monkeypatch.setattr(llm,'get_client',factory)
    monkeypatch.setattr(pool.settings,'catalog_workspace_reuse_model_client',True)
    async def run():
        try:
            with pytest.raises(ValueError):
                async with pool.borrow_client():raise ValueError('request failed')
            async with pool.borrow_client() as c:assert c is context.__aenter__.return_value
            assert factory.call_count==1
        finally:await pool.close_client()
    asyncio.run(run())
