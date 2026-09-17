"""Application-owned connection pool; no dialogue or generated-result cache."""
import asyncio
from contextlib import asynccontextmanager
import logging
import time
from .settings import settings

_context=None
_client=None
_lock=None


def lock():
    global _lock
    if _lock is None:_lock=asyncio.Lock()
    return _lock


@asynccontextmanager
async def borrow_client():
    from .llm import get_client
    global _context,_client
    if not settings.catalog_workspace_reuse_model_client:
        async with get_client() as client:yield client
        return
    async with lock():
        if _client is None:
            context=get_client()
            client=await context.__aenter__()
            _context,_client=context,client
    yield _client


async def close_client():
    global _context,_client,_lock
    async with lock():
        context=_context;_context=_client=None
        if context is not None:await context.__aexit__(None,None,None)
    _lock=None


async def warm_startup():
    if not settings.catalog_workspace_reuse_model_client:return
    from .catalog_conversation import plan_turn
    started=time.perf_counter()
    # A greeting initializes the routing prompt and connection. No search,
    # user state, corpus evidence or benchmark query is sent or created.
    try:
        _,receipt=await plan_turn('你好',{'messages':[]})
        logging.getLogger(__name__).info('catalog route startup warmup %.3fs response=%s',
            time.perf_counter()-started,receipt.get('responseId'))
    except Exception as exc:
        logging.getLogger(__name__).warning('catalog route startup warmup unavailable: %s',type(exc).__name__)
