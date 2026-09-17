import asyncio
from pathlib import Path
import sys
import time
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_bridge import LiveBridge, experiment_settings
from app.catalog_evidence import CatalogBinding, CatalogSearchRequest
from app.settings import settings

BINDING = CatalogBinding(dataRoot='D:/fixture', runId='fixture', manifestSha256='0'*64)


def fixture_worker(connection, mode):
    connection.send({'kind': 'ready', 'strategy': {'fixture': True}})
    while True:
        request = connection.recv()
        if mode == 'hang':
            time.sleep(30)
        if mode == 'crash':
            return
        connection.send({'kind': 'result', 'result': {'hits': [], 'query': request['query']}})


def request(query='测试'):
    return CatalogSearchRequest(query=query, source='kuaisearch', limit=10, binding=BINDING)


@pytest.mark.parametrize('mode', ['hang', 'crash'])
def test_timeout_and_process_exit_kill_worker_without_late_result(mode):
    async def run():
        bridge = LiveBridge(mode, worker=fixture_worker)
        try:
            await bridge.start(30)
            with pytest.raises((TimeoutError, RuntimeError)):
                await bridge.provider(BINDING, timeout=.1)(request())
            assert bridge.closed and not bridge.process.is_alive()
            with pytest.raises(ValueError):
                await bridge.provider(BINDING)(request('下一条'))
        finally:
            bridge.close()
    asyncio.run(run())


def test_cancellation_stops_worker():
    async def run():
        bridge = LiveBridge('hang', worker=fixture_worker)
        try:
            await bridge.start(30)
            task = asyncio.create_task(bridge.provider(BINDING)(request()))
            await asyncio.sleep(.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert bridge.closed and not bridge.process.is_alive()
        finally:
            bridge.close()
    asyncio.run(run())


def test_serial_responses_and_settings_restored_on_exception():
    original = settings.model_dump()
    with pytest.raises(RuntimeError):
        with experiment_settings(BINDING, 'multicpr'):
            assert settings.catalog_evidence_source == 'multicpr'
            raise RuntimeError('fixture')
    assert settings.model_dump() == original

    async def run():
        bridge = LiveBridge('ok', worker=fixture_worker)
        try:
            await bridge.start(30)
            rows = await asyncio.gather(*(bridge.provider(BINDING)(request(q)) for q in ['甲', '乙']))
            assert [r['query'] for r in rows] == ['甲', '乙']
            wrong = request().model_copy(update={'binding': BINDING.model_copy(update={'run_id': 'wrong'})})
            with pytest.raises(ValueError):
                await bridge.provider(BINDING)(wrong)
        finally:
            bridge.close()
    asyncio.run(run())
