"""Agent-side read client: no local GPU worker and no transaction authority."""
import asyncio
from contextlib import suppress
import time
from urllib.parse import urlsplit
import uuid

import httpx

from .catalog_service import document_scope, fingerprint, verify_scope
from .settings import settings


class RemoteCatalogService:
    def __init__(self, *, url=None, token=None, transport=None):
        self.url=(url or settings.catalog_search_service_url).rstrip('/')
        parsed=urlsplit(self.url)
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('invalid_internal_search_url')
        if token is None:
            from .catalog_search_server import load_token
            token=load_token()
        self.headers={'X-Search-Service-Token':token}
        self.transport=transport
        self.failures=0
        self.open_until=0.0

    async def start(self):
        # Search availability must not prevent login/order/payment startup.
        return None

    def close(self):
        # Each request owns its HTTP client, not the independent search process.
        pass

    def client(self,timeout):
        return httpx.AsyncClient(base_url=self.url,headers=self.headers,transport=self.transport,
            timeout=timeout,trust_env=False,follow_redirects=False)

    async def cancel(self,request_id):
        with suppress(Exception):
            async with self.client(2) as client:
                await client.delete('/v1/search/'+request_id)

    async def search(self,query,*,requirements=None,retrieval_query=None):
        if time.monotonic()<self.open_until:raise RuntimeError('search_circuit_open')
        from .catalog_search_server import SearchCommand
        body=SearchCommand(requestId=uuid.uuid4().hex,query=query,
            retrievalQuery=retrieval_query,requirements=requirements or []).model_dump()
        try:
            async with asyncio.timeout(settings.catalog_workspace_timeout_seconds+2):
                async with self.client(httpx.Timeout(settings.catalog_workspace_timeout_seconds,connect=2)) as client:
                    response=await client.post('/v1/search',json=body)
                    response.raise_for_status()
            if len(response.content)>8*1024*1024:raise ValueError('search_response_too_large')
            result=response.json()
            if result.get('protocol')!='catalog.search.v1' or result.get('requestId')!=body['requestId'] or result.get('requestSha256')!=fingerprint(body):
                raise ValueError('search_response_identity_mismatch')
            scope=result['scope'];verify_scope(scope)
            if scope['query']!=query or scope['requirements']!=body['requirements']:
                raise ValueError('search_response_query_mismatch')
            # Recompute presentation from source-bound rows; never trust remote purchase claims.
            if scope!=document_scope(query,scope['sources'],scope['bindingSha256'],body['requirements']):
                raise ValueError('search_response_scope_mismatch')
            self.failures=0
            return scope
        except asyncio.CancelledError:
            await asyncio.shield(self.cancel(body['requestId']))
            raise
        except Exception:
            self.failures+=1
            if self.failures>=3:self.open_until=time.monotonic()+10
            await self.cancel(body['requestId'])
            raise
