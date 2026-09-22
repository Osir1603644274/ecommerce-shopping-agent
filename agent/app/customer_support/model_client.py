"""Application-owned SDK transport. No conversation or answer cache, no warmup call."""
import asyncio
from contextlib import asynccontextmanager

_client=None
_context=None
_lock=None

def create_client():
    from ..llm import get_client
    return get_client()

def lock():
    global _lock
    if _lock is None:_lock=asyncio.Lock()
    return _lock

@asynccontextmanager
async def borrow_client():
    global _client,_context
    async with lock():
        if _client is None:
            context=create_client()
            _client=await context.__aenter__();_context=context
        client=_client
    yield client

async def initialize_client():
    # Constructs the SDK/SSL context only. Never sends a provider request.
    async with borrow_client():pass

async def close_client():
    global _client,_context,_lock
    async with lock():
        context=_context;_client=_context=None
        if context is not None:await context.__aexit__(None,None,None)
    _lock=None
