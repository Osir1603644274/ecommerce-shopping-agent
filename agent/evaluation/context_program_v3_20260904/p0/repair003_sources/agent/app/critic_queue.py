"""EvidenceCritic bounded background queue for shadow-mode execution.

Does NOT block the chat response.  Enqueue the critic job and let the worker
pick it up asynchronously.  Results are written back to the AgentRunTrace
via its runId.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 100


class _CriticJob:
    """One queued critic job — minimal payload to avoid unbounded memory."""

    __slots__ = ("run_id", "draft", "candidates", "semantic", "enqueued_at")

    def __init__(
        self,
        run_id: str,
        draft: dict[str, Any],
        candidates: list[dict[str, Any]],
        semantic: bool = False,
    ) -> None:
        self.run_id = run_id
        self.draft = draft
        self.candidates = candidates
        self.semantic = semantic
        import time
        self.enqueued_at = time.monotonic()


class CriticQueue:
    """Bounded asyncio queue for EvidenceCritic jobs.

    When full, new jobs are dropped with a skipped/degraded log.
    The worker drains jobs one at a time and writes results back to
    the AgentRunTrace via runId.
    """

    def __init__(self, max_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self._queue: asyncio.Queue[_CriticJob | None] = asyncio.Queue(
            maxsize=max_size
        )
        self._worker_task: asyncio.Task[None] | None = None
        self._running = False
        self._skipped = 0
        self._completed = 0
        self._max_size = max_size

    @property
    def skipped(self) -> int:
        return self._skipped

    @property
    def completed(self) -> int:
        return self._completed

    async def enqueue(
        self,
        run_id: str,
        draft: dict[str, Any],
        candidates: list[dict[str, Any]],
        *,
        semantic: bool = False,
    ) -> bool:
        """Try to enqueue a critic job. Returns False if the queue is full."""
        job = _CriticJob(run_id, draft, candidates, semantic)
        try:
            self._queue.put_nowait(job)
            return True
        except asyncio.QueueFull:
            self._skipped += 1
            logger.warning(
                "CriticQueue full (%d/%d); skipping job for run=%s",
                self._max_size,
                self._max_size,
                run_id,
            )
            # Record the skip in the trace
            try:
                from .agent_trace import CriticTrace, get_trace_store
                trace = await get_trace_store().get(run_id)
                if trace is not None:
                    trace.critic = CriticTrace(
                        skipped=True,
                        skip_reason=f"queue_full_{self._max_size}",
                    )
                    await get_trace_store().save(trace)
            except Exception:
                pass
            return False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker())

    async def stop(self, *, graceful: bool = True) -> None:
        if self._worker_task is None:
            self._running = False
            return
        if graceful:
            from .settings import settings

            timeout = max(
                float(settings.evidence_critic_shutdown_timeout_seconds), 0.1
            )
            try:
                await asyncio.wait_for(self._queue.join(), timeout=timeout)
                await self._queue.put(None)
                await asyncio.wait_for(self._worker_task, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "CriticQueue did not drain within %.2fs; cancelling worker",
                    timeout,
                )
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except asyncio.CancelledError:
                    pass
                while True:
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    else:
                        self._queue.task_done()
        else:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._worker_task = None
        self._running = False

    async def _worker(self) -> None:
        from .agent_trace import CriticTrace, get_trace_store
        from .evidence_critic import run_evidence_critic, critic_enabled

        while True:
            try:
                job = await self._queue.get()
            except asyncio.CancelledError:
                break

            if job is None:  # graceful shutdown sentinel
                self._queue.task_done()
                break

            if not critic_enabled():
                self._queue.task_done()
                continue

            import time
            start = time.perf_counter()
            try:
                from .settings import settings

                critic_output = await asyncio.wait_for(
                    run_evidence_critic(
                        job.draft,
                        job.candidates,
                        semantic=job.semantic,
                    ),
                    timeout=max(
                        float(settings.evidence_critic_timeout_seconds), 0.1
                    ),
                )
                duration = (time.perf_counter() - start) * 1000
                trace = await get_trace_store().get(job.run_id)
                if trace is not None:
                    trace.critic = CriticTrace(
                        approved=critic_output.approved,
                        issue_count=len(critic_output.issues),
                        issue_codes=[i.code.value for i in critic_output.issues],
                        critic_version=critic_output.critic_version,
                        duration_ms=round(duration, 2),
                    )
                    await get_trace_store().save(trace)
                self._completed += 1
            except Exception:
                logger.exception("Critic worker failed for run=%s", job.run_id)
                try:
                    trace = await get_trace_store().get(job.run_id)
                    if trace is not None:
                        trace.critic = CriticTrace(
                            skipped=True,
                            skip_reason="worker_error",
                        )
                        await get_trace_store().save(trace)
                except Exception:
                    pass
            finally:
                self._queue.task_done()


# Singleton queue
_critic_queue: CriticQueue | None = None


def get_critic_queue() -> CriticQueue:
    global _critic_queue
    if _critic_queue is None:
        _critic_queue = CriticQueue()
    return _critic_queue
