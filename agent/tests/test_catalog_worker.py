"""Real spawned process/IPC faults, with synthetic work instead of GPU ranking."""
import asyncio
import time
import pytest

from app.catalog_worker import LiveBridge
from app.catalog_evidence import CatalogBinding, CatalogSearchRequest

BINDING=CatalogBinding(dataRoot='D:/fixture',runId='fixture',manifestSha256='0'*64)


def fixture_worker(connection,mode):
    connection.send({'kind':'ready','strategy':{'fixture':True}})
    while True:
        request=connection.recv()
        if mode=='hang':time.sleep(30)
        if mode=='crash':return
        connection.send({'kind':'result','result':{'hits':[], 'query':request['query']}})


def request(query='测试'):
    return CatalogSearchRequest(query=query,source='kuaisearch',limit=10,binding=BINDING)


@pytest.mark.parametrize('mode',['hang','crash','cancel'])
def test_real_worker_fault_leaves_no_late_response(mode):
    async def run():
        bridge=LiveBridge('hang' if mode=='cancel' else mode,worker=fixture_worker)
        try:
            await bridge.start(30)
            job=asyncio.create_task(bridge.provider(BINDING,timeout=.15)(request()))
            if mode=='cancel':
                await asyncio.sleep(.05)
                job.cancel()
            with pytest.raises((TimeoutError,RuntimeError,asyncio.CancelledError)):
                await job
            assert bridge.closed and not bridge.process.is_alive()
            with pytest.raises(ValueError):await bridge.provider(BINDING)(request('下一条'))
        finally:bridge.close()
    asyncio.run(run())


def test_real_ipc_serializes_queries():
    async def run():
        bridge=LiveBridge('ok',worker=fixture_worker)
        try:
            await bridge.start(30)
            observed=[]
            provider=bridge.provider(BINDING,on_result=lambda req,res,_: observed.append((req.query,res['query'])))
            await asyncio.gather(*(provider(request(q)) for q in ['甲','乙']))
            assert observed==[('甲','甲'),('乙','乙')]
        finally:bridge.close()
    asyncio.run(run())


def test_waiting_owner_timeout_does_not_close_active_worker(monkeypatch):
    from app.catalog_service import CatalogService,settings
    async def run():
        service=CatalogService()
        sentinel=object();service.bridge=sentinel
        monkeypatch.setattr(settings,'catalog_workspace_timeout_seconds',.02)
        await service.queue.acquire()
        try:
            with pytest.raises(TimeoutError):await service.search('等待中的请求')
            assert service.bridge is sentinel
        finally:service.queue.release()
    asyncio.run(run())
