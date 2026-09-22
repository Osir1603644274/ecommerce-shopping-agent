import asyncio
from app.customer_support import model_client
from .test_commerce_workspace import async_test

@async_test
async def test_initialization_reuses_transport_and_shutdown_closes_once(monkeypatch):
    events=[]
    class Client:
        async def __aenter__(self):events.append('enter');return self
        async def __aexit__(self,*args):events.append('close')
    def factory():events.append('construct');return Client()
    monkeypatch.setattr(model_client,'create_client',factory)
    try:
        await model_client.initialize_client()
        assert events==['construct','enter']
        async def borrow():
            async with model_client.borrow_client() as client:return client
        clients=await asyncio.gather(borrow(),borrow())
        assert clients[0] is clients[1] and events==['construct','enter']
    finally:await model_client.close_client()
    assert events==['construct','enter','close']
    await model_client.close_client()
    assert events.count('close')==1
