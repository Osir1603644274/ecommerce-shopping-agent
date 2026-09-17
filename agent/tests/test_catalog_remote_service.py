"""Distributed retrieval contract tests; doubles do not claim real retrieval quality."""
import asyncio
from copy import deepcopy
import json
import uuid

import httpx
import pytest

from app.catalog_remote import RemoteCatalogService
from app.catalog_search_server import create_app,load_token
from app.catalog_service import document_scope,fingerprint
from app.settings import settings

TOKEN='test-only-'+('a'*40)


def evidence(query,requirements=None):
    sources=[]
    for source in ('kuaisearch','multicpr'):
        docid=source+':1'
        sources.append({'source':source,'hits':[{'source':source,'docid':docid,'rank':1,'score':1.0}],
            'metadata':[{'docid':docid,'fields':{'title':{'value':'木质收纳盒'},'brand':{'value':None},'seller':{'value':None}},
                         'provenance':{'recordSha256':'a'*64}}]})
    return document_scope(query,sources,'b'*64,requirements or [])


class Engine:
    def __init__(self):self.closed=False;self.calls=0
    async def start(self):pass
    async def search(self,query,*,requirements=None,retrieval_query=None):
        self.calls+=1
        return evidence(query,requirements)
    def close(self):self.closed=True


def test_remote_contract_preserves_scope_and_has_no_transaction_routes():
    async def run():
        engine=Engine();app=create_app(lambda:engine,token=TOKEN)
        async with app.router.lifespan_context(app):
            transport=httpx.ASGITransport(app=app)
            remote=RemoteCatalogService(url='http://search',token=TOKEN,transport=transport)
            requirements=[{'facet':'材质','mode':'require','value':'木质','terms':[]}]
            result=await remote.search('木质收纳盒',requirements=requirements)
            assert result==evidence('木质收纳盒',requirements)
            assert result['commerceAuthority'] is False and engine.calls==1
            remote.close();assert not engine.closed
            async with httpx.AsyncClient(transport=transport,base_url='http://search') as client:
                assert (await client.get('/health')).json()['protocol']=='catalog.search.v1'
                assert (await client.post('/api/orders',json={})).status_code==404
        assert engine.closed
    asyncio.run(run())


@pytest.mark.parametrize('header',[{}, {'X-Search-Service-Token':'wrong'}])
def test_internal_identity_required(header):
    async def run():
        engine=Engine();app=create_app(lambda:engine,token=TOKEN)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://search') as client:
                body={'requestId':uuid.uuid4().hex,'query':'木盒'}
                assert (await client.post('/v1/search',json=body,headers=header)).status_code==401
                assert (await client.delete('/v1/search/'+body['requestId'],headers=header)).status_code==401
                assert engine.calls==0
    asyncio.run(run())


@pytest.mark.parametrize('body',[
    {'requestId':'not-an-id','query':'盒子'},
    {'requestId':'a'*32,'query':'x'*2001},
    {'requestId':'a'*32,'query':'盒子','requirements':[{'facet':'材质','mode':'invent','value':'木'}]},
    {'requestId':'a'*32,'query':'盒子','purchase':True},
])
def test_invalid_command_rejected(body):
    async def run():
        engine=Engine();app=create_app(lambda:engine,token=TOKEN)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://search',headers={'X-Search-Service-Token':TOKEN}) as client:
                assert (await client.post('/v1/search',json=body)).status_code==422
                assert engine.calls==0
    asyncio.run(run())


def test_capacity_duplicate_and_targeted_cancel_do_not_cancel_other_job():
    class WaitingEngine(Engine):
        async def search(self,query,**kwargs):
            await asyncio.sleep(60)
    async def run():
        engine=WaitingEngine();app=create_app(lambda:engine,token=TOKEN,capacity=2)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://search',headers={'X-Search-Service-Token':TOKEN}) as client:
                ids=[uuid.uuid4().hex for _ in range(3)]
                first=asyncio.create_task(client.post('/v1/search',json={'requestId':ids[0],'query':'A'}))
                second=asyncio.create_task(client.post('/v1/search',json={'requestId':ids[1],'query':'B'}))
                for _ in range(100):
                    if len(app.state.jobs)==2:break
                    await asyncio.sleep(.01)
                assert len(app.state.jobs)==2
                assert (await client.post('/v1/search',json={'requestId':ids[0],'query':'A'})).status_code==409
                assert (await client.post('/v1/search',json={'requestId':ids[2],'query':'C'})).status_code==429
                await client.delete('/v1/search/'+ids[0])
                assert (await first).status_code==409
                assert ids[1] in app.state.jobs and not app.state.jobs[ids[1]][1].cancelled()
                await client.delete('/v1/search/'+ids[1])
                assert (await second).status_code==409
                assert not app.state.jobs
    asyncio.run(run())


@pytest.mark.parametrize('corruption',['request','hash','scope','purchase','query'])
def test_remote_rejects_wrong_identity_or_modified_evidence(corruption):
    def reply(request):
        if request.method=='DELETE':return httpx.Response(200,json={})
        body=json.loads(request.content);scope=evidence(body['query'])
        result={'protocol':'catalog.search.v1','requestId':body['requestId'],'requestSha256':fingerprint(body),'scope':scope}
        if corruption=='request':result['requestId']='wrong'
        if corruption=='hash':result['requestSha256']='wrong'
        if corruption=='scope':scope['groups'][0]['title']='invented'
        if corruption=='purchase':scope['commerceAuthority']=True
        if corruption=='query':result['scope']=evidence('other')
        return httpx.Response(200,json=result)
    async def run():
        remote=RemoteCatalogService(url='http://search',token=TOKEN,transport=httpx.MockTransport(reply))
        with pytest.raises(ValueError):await remote.search('盒子')
    asyncio.run(run())


def test_remote_failure_opens_circuit_without_startup_dependency():
    posts=[]
    def reply(request):
        if request.method=='POST':posts.append(request)
        return httpx.Response(503,json={'detail':'unavailable'})
    async def run():
        remote=RemoteCatalogService(url='http://search',token=TOKEN,transport=httpx.MockTransport(reply))
        await remote.start()
        for _ in range(3):
            with pytest.raises(httpx.HTTPStatusError):await remote.search('盒子')
        with pytest.raises(RuntimeError,match='circuit_open'):await remote.search('盒子')
        assert len(posts)==3
    asyncio.run(run())


def test_worker_deadline_frees_slot(monkeypatch):
    class WaitingEngine(Engine):
        async def search(self,*args,**kwargs):await asyncio.sleep(60)
    monkeypatch.setattr(settings,'catalog_workspace_timeout_seconds',.01)
    async def run():
        app=create_app(WaitingEngine,token=TOKEN)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://search',headers={'X-Search-Service-Token':TOKEN}) as client:
                assert (await client.post('/v1/search',json={'requestId':uuid.uuid4().hex,'query':'盒子'})).status_code==504
                assert not app.state.jobs
    asyncio.run(run())


def test_token_file_required(monkeypatch,tmp_path):
    monkeypatch.setattr(settings,'catalog_search_token_file','')
    with pytest.raises(RuntimeError):load_token()
    path=tmp_path/'secret';path.write_text('short')
    monkeypatch.setattr(settings,'catalog_search_token_file',str(path))
    with pytest.raises(RuntimeError):load_token()
    path.write_text(TOKEN);assert load_token()==TOKEN
