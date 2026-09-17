"""Process-level API harness with isolated in-memory control-plane storage.

This module is only an observability test entry point.  It runs the real FastAPI
routes and Agent runtime while replacing Redis-backed TaskState/session/trace
stores with the repository's deterministic FakeRedis implementation.
"""

from app import agent_trace, session_memory, task_state
from app.agent_trace import TraceStore
from app.main import app
from tests.fake_redis import FakeRedis


_isolated_redis = FakeRedis()
session_memory._client = _isolated_redis
task_state._client = _isolated_redis
task_state._task_locks.clear()
task_state._session_locks.clear()
agent_trace.set_trace_store(TraceStore(client=_isolated_redis))


__all__ = ["app"]
