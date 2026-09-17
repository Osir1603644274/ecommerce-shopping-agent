"""Independent read-only retrieval API. No transaction client or model orchestration.

Run with python -m uvicorn agent.app.catalog_search_server:app --host 127.0.0.1
--port 18110. The server owns GPU worker lifetime; caller cancellation never
terminates another caller's work. In-flight read jobs are intentionally ephemeral.
"""
import asyncio
from contextlib import asynccontextmanager, suppress
import hmac
from pathlib import Path
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .catalog_conversation import CatalogRequirement
from .catalog_service import CatalogService, fingerprint
from .settings import settings


class SearchCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    requestId: str = Field(pattern=r'^[a-f0-9]{32}$')
    query: str = Field(min_length=1, max_length=2000)
    retrievalQuery: str | None = Field(default=None, min_length=1, max_length=2000)
    requirements: list[CatalogRequirement] = Field(default_factory=list, max_length=32)


def load_token():
    path=settings.catalog_search_token_file
    if not path:raise RuntimeError('search_service_token_file_required')
    token=Path(path).read_text(encoding='utf8').strip()
    if len(token)<32:raise RuntimeError('search_service_token_too_short')
    return token


def create_app(engine_factory=CatalogService, *, token=None, capacity=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.token=token or load_token()
        app.state.engine=engine_factory()
        app.state.jobs={}
        app.state.instance=uuid.uuid4().hex
        app.state.capacity=capacity or settings.catalog_search_max_inflight
        await app.state.engine.start()
        try:yield
        finally:
            tasks=[task for _,task in app.state.jobs.values()]
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            app.state.engine.close()

    app=FastAPI(title='Independent catalog search',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)

    async def authorized(request:Request,x_search_service_token:str=Header(default='')):
        if not hmac.compare_digest(request.app.state.token,x_search_service_token):
            raise HTTPException(401,'internal_search_identity_required')

    @app.get('/health')
    async def health():
        return {'status':'UP','protocol':'catalog.search.v1','instanceId':app.state.instance}

    @app.post('/v1/search',dependencies=[Depends(authorized)])
    async def search(command:SearchCommand,request:Request):
        jobs=app.state.jobs
        if command.requestId in jobs:raise HTTPException(409,'request_already_running')
        if len(jobs)>=app.state.capacity:raise HTTPException(429,'search_capacity_exceeded',headers={'Retry-After':'2'})
        body=command.model_dump()
        async def run():
            async with asyncio.timeout(settings.catalog_workspace_timeout_seconds):
                return await app.state.engine.search(command.query,requirements=body['requirements'],retrieval_query=command.retrievalQuery)
        task=asyncio.create_task(run())
        jobs[command.requestId]=(fingerprint(body),task)
        try:
            while not task.done():
                done,_=await asyncio.wait({task},timeout=0.1)
                if not done and await request.is_disconnected():
                    task.cancel();raise HTTPException(499,'caller_disconnected')
            result=await task
            return {'protocol':'catalog.search.v1','requestId':command.requestId,
                    'requestSha256':fingerprint(body),'instanceId':app.state.instance,'scope':result}
        except asyncio.CancelledError:
            raise HTTPException(409,'search_cancelled')
        except TimeoutError:
            raise HTTPException(504,'search_deadline_exceeded')
        except HTTPException:raise
        except Exception:
            raise HTTPException(503,'search_unavailable')
        finally:
            if not task.done():task.cancel()
            with suppress(asyncio.CancelledError,Exception):await task
            jobs.pop(command.requestId,None)

    @app.delete('/v1/search/{request_id}',dependencies=[Depends(authorized)])
    async def cancel(request_id:str):
        job=app.state.jobs.get(request_id)
        if job:job[1].cancel()
        return {'requestId':request_id,'status':'cancellation_requested' if job else 'not_running'}

    return app


app=create_app()
