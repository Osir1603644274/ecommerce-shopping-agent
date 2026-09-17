import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..task_state import (
    TaskState,
    TaskStateCreateRequest,
    TaskStateEvent,
    TaskStateNotFoundError,
    TaskStatePatchRequest,
    TaskStateRevisionConflictError,
    TaskStateTraceCallback,
    TaskStateTransitionError,
    create_task_state,
    get_task_state,
    list_task_events,
    update_task_state,
)

logger = logging.getLogger(__name__)


def _sse_data(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _task_state_trace_response(
    operation: Callable[[TaskStateTraceCallback], Awaitable[TaskState]],
) -> StreamingResponse:
    async def event_stream():
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def trace(event: dict[str, Any]) -> None:
            await queue.put({"type": "step", **event})

        async def produce() -> None:
            try:
                state = await operation(trace)
                await queue.put(
                    {
                        "type": "complete",
                        "state": state.model_dump(by_alias=True, mode="json"),
                    }
                )
            except TaskStateNotFoundError:
                await queue.put(
                    {
                        "type": "error",
                        "code": 404,
                        "message": "TaskState 不存在或已过期",
                    }
                )
            except TaskStateRevisionConflictError as exc:
                await queue.put(
                    {
                        "type": "error",
                        "code": 409,
                        "message": "TaskState revision 冲突",
                        "expectedRevision": exc.expected,
                        "actualRevision": exc.actual,
                    }
                )
            except TaskStateTransitionError as exc:
                await queue.put(
                    {"type": "error", "code": 422, "message": str(exc)}
                )
            except Exception:
                logger.exception("TaskState trace 执行失败")
                await queue.put(
                    {
                        "type": "error",
                        "code": 500,
                        "message": "TaskState trace 执行失败",
                    }
                )
            finally:
                await queue.put(None)

        producer = asyncio.create_task(produce())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield _sse_data(event)
        finally:
            if not producer.done():
                producer.cancel()
                with suppress(asyncio.CancelledError):
                    await producer

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


router = APIRouter(prefix="/agent/tasks", tags=["任务状态"])


def _require_public_create_boundary(request: TaskStateCreateRequest) -> None:
    if request.domain_state:
        raise HTTPException(
            status_code=403,
            detail="domainState 只能由 Agent 服务端创建",
        )


def _require_public_patch_boundary(request: TaskStatePatchRequest) -> None:
    planner_field_requested = bool(
        {"active_plan", "planning_failure"}.intersection(request.model_fields_set)
    )
    if (
        request.actor != "user"
        or request.domain_state_patch
        or planner_field_requested
    ):
        raise HTTPException(
            status_code=403,
            detail="服务端 TaskState 字段不能通过公开接口写入",
        )


@router.post(
    "",
    response_model=TaskState,
    status_code=201,
    summary="创建最小 TaskState",
)
async def create_task(request: TaskStateCreateRequest) -> TaskState:
    _require_public_create_boundary(request)
    return await create_task_state(request)


@router.post(
    "/trace",
    summary="创建 TaskState 并以 SSE 输出学习 trace",
)
async def trace_create_task(
    request: TaskStateCreateRequest,
) -> StreamingResponse:
    _require_public_create_boundary(request)
    return _task_state_trace_response(
        lambda trace: create_task_state(request, trace=trace)
    )


@router.get(
    "/{task_id}",
    response_model=TaskState,
    summary="读取 TaskState 最新快照",
)
async def get_task(task_id: str) -> TaskState:
    state = await get_task_state(task_id)
    if state is None:
        raise HTTPException(status_code=404, detail="TaskState 不存在或已过期")
    return state


@router.patch(
    "/{task_id}",
    response_model=TaskState,
    summary="通过受控 Patch 更新 TaskState",
)
async def patch_task(
    task_id: str,
    request: TaskStatePatchRequest,
) -> TaskState:
    _require_public_patch_boundary(request)
    try:
        return await update_task_state(task_id, request)
    except TaskStateNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail="TaskState 不存在或已过期",
        ) from exc
    except TaskStateRevisionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "TaskState revision 冲突，请先重新读取最新状态",
                "expectedRevision": exc.expected,
                "actualRevision": exc.actual,
            },
        ) from exc
    except TaskStateTransitionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch(
    "/{task_id}/trace",
    summary="更新 TaskState 并以 SSE 输出学习 trace",
)
async def trace_patch_task(
    task_id: str,
    request: TaskStatePatchRequest,
) -> StreamingResponse:
    _require_public_patch_boundary(request)
    return _task_state_trace_response(
        lambda trace: update_task_state(task_id, request, trace=trace)
    )


@router.get(
    "/{task_id}/events",
    response_model=list[TaskStateEvent],
    summary="读取 TaskState 事件日志",
)
async def get_task_events(task_id: str) -> list[TaskStateEvent]:
    try:
        return await list_task_events(task_id)
    except TaskStateNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail="TaskState 不存在或已过期",
        ) from exc


__all__ = ["router"]
