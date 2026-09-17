"""Application-owned Java connection pool; request-local identity and deadlines."""
from contextlib import asynccontextmanager
from http.cookiejar import CookieJar, DefaultCookiePolicy
import httpx

_client: httpx.AsyncClient | None = None

class NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

async def start_java_client():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=5.0, follow_redirects=False,
            cookies=CookieJar(policy=NoCookies()),
            limits=httpx.Limits(max_connections=100,max_keepalive_connections=20))

async def close_java_client():
    global _client
    client,_client=_client,None
    if client is not None:await client.aclose()

class _DeadlineClient:
    def __init__(self,client,timeout):self.client,self.timeout=client,timeout
    async def request(self,method,url,**kwargs):
        kwargs.setdefault('timeout',self.timeout)
        return await self.client.request(method,url,**kwargs)
    async def get(self,url,**kwargs):return await self.request('GET',url,**kwargs)

@asynccontextmanager
async def java_connection(timeout=5.0):
    if _client is not None:
        yield _DeadlineClient(_client,timeout)
    else:
        # Standalone tests/callers without FastAPI lifespan own their short-lived client.
        async with httpx.AsyncClient(timeout=timeout) as client:yield client
