import pytest
from live_evaluation import LiveDriver
import live_evaluation

def test_nested_driver_counts_wall_once_and_excludes_customer_http(monkeypatch):
    d=object.__new__(LiveDriver)
    d.drive_depth=0;d.chat_observations=[];d.first_request_started=99
    d.simulator_wait_ms=0;d.fixture_control_ms=0
    times=iter([100,100.005,100.020])
    monkeypatch.setattr(live_evaluation.time,'perf_counter',lambda:next(times))
    def run(action,result):
        if action=='outer':d.drive('inner',None)
        else:d.chat_observations.append({'elapsedMs':5})
    d._drive=run;d.drive('outer',None)
    assert d.simulator_wait_ms==pytest.approx(15)
    assert d.fixture_control_ms==0 and d.drive_depth==0
